import re
import gzip
import json
from collections import Counter
import sqlite3

import numpy as np
import pandas as pd
from lxml import etree

from .shared_utils.util import log

class UnirefToSqliteParser:

    def __init__(self, uniref_xml_gz_file_path, go_annotations_meta, sqlite_file_path, verbose = True, log_progress_every = 1000, \
            chunk_size = 100000):
        
        self.uniref_xml_gz_file_path = uniref_xml_gz_file_path
        self.go_annotations_meta = go_annotations_meta
        self.sqlite_conn = sqlite3.connect(sqlite_file_path)
        self.verbose = verbose
        self.log_progress_every = log_progress_every
        self.chunk_size = chunk_size
        
        self._process_go_annotations_meta()
        
        self.go_index_record_counter = Counter()
        self.unrecognized_go_annotations = Counter()
        self.n_records_with_any_go_annotation = 0
                
        self._chunk_indices = []
        self._chunk_records = []
        
    def parse(self):
        
        with gzip.open(self.uniref_xml_gz_file_path, 'rb') as f:
            context = etree.iterparse(f, tag = UnirefToSqliteParser._NAMESPACE_PREFIX + 'entry', events = ('end',))
            _etree_fast_iter(context, self._process_entry)
        
        if len(self._chunk_records) > 0:
            self._save_current_chunk()
            
        if self.verbose:
            log('Ignored the following unrecognized GO annotations: %s' % self.unrecognized_go_annotations)
            log('Parsed %d records with any GO annotation.' % self.n_records_with_any_go_annotation)
            
        go_id_record_counter = pd.Series(self.go_index_record_counter)
        go_id_record_counter.index = [self.go_index_to_id[index] for index in go_id_record_counter.index]

        self.go_annotations_meta['count'] = go_id_record_counter.reindex(self.go_annotations_meta.index).fillna(0)
        self.go_annotations_meta['freq'] = self.go_annotations_meta['count'] / self.n_records_with_any_go_annotation
              
        if self.verbose:
            log('Done.')
            
    def _process_go_annotations_meta(self):
        self.go_annotation_to_all_ancestors = self.go_annotations_meta['all_ancestors'].to_dict()
        self.go_id_to_index = self.go_annotations_meta['index'].to_dict()
        self.go_index_to_id = self.go_annotations_meta.reset_index().set_index('index')['id'].to_dict()
        
    def _process_entry(self, i, event, entry):
            
        if self.verbose and i % self.log_progress_every == 0:
            log(i, end = '\r')

        repr_member, = entry.xpath(r'uniprot:representativeMember', namespaces = UnirefToSqliteParser._NAMESPACES)
        db_ref, = repr_member.xpath(r'uniprot:dbReference', namespaces = UnirefToSqliteParser._NAMESPACES)
        protein_name = db_ref.attrib['id']

        try:
            taxonomy_element, = db_ref.xpath(r'uniprot:property[@type="NCBI taxonomy"]', namespaces = UnirefToSqliteParser._NAMESPACES)
            tax_id = int(taxonomy_element.attrib['value'])
        except:
            tax_id = np.nan

        extracted_go_annotations = {category: UnirefToSqliteParser._extract_go_category(entry, category) for category in UnirefToSqliteParser._GO_ANNOTATION_CATEGORIES}
        
        self._chunk_indices.append(i)
        self._chunk_records.append((tax_id, protein_name, extracted_go_annotations))
        
        if len(self._chunk_records) >= self.chunk_size:
            self._save_current_chunk()

    def _save_current_chunk(self):
        
        chunk_records_df = pd.DataFrame(self._chunk_records, columns = ['tax_id', 'uniprot_name', 'go_annotations'], index = self._chunk_indices)
        
        chunk_records_df['flat_go_annotations'] = chunk_records_df['go_annotations'].apply(\
                lambda go_annotations: list(sorted(set.union(*map(set, go_annotations.values())))))
        chunk_records_df['n_go_annotations'] = chunk_records_df['flat_go_annotations'].apply(len)
        chunk_records_df['complete_go_annotation_indices'] = chunk_records_df['flat_go_annotations'].apply(self._get_complete_go_annotation_indices)
        chunk_records_df['n_complete_go_annotations'] = chunk_records_df['complete_go_annotation_indices'].apply(len)
        self.n_records_with_any_go_annotation += (chunk_records_df['n_complete_go_annotations'] > 0).sum()
        
        for complete_go_annotation_indices in chunk_records_df['complete_go_annotation_indices']:
            self.go_index_record_counter.update(complete_go_annotation_indices)

        chunk_records_df['go_annotations'] = chunk_records_df['go_annotations'].apply(json.dumps)
        chunk_records_df['flat_go_annotations'] = chunk_records_df['flat_go_annotations'].apply(json.dumps)
        chunk_records_df['complete_go_annotation_indices'] = chunk_records_df['complete_go_annotation_indices'].apply(json.dumps)
        chunk_records_df.to_sql('protein_annotations', self.sqlite_conn, if_exists = 'append')
        
        self._chunk_indices = []
        self._chunk_records = []
        
    def _get_complete_go_annotation_indices(self, go_annotations):
        complete_go_annotations = self._get_complete_go_annotations(go_annotations)
        return list(sorted(filter(None, map(self.go_id_to_index.get, go_annotations))))

    def _get_complete_go_annotations(self, go_annotations):
        return set.union(set(), *[self._get_go_annotation_all_ancestors(annotation) for annotation in go_annotations])
    
    def _get_go_annotation_all_ancestors(self, annotation):
        if annotation in self.go_annotation_to_all_ancestors:
            return self.go_annotation_to_all_ancestors[annotation]
        else:
            self.unrecognized_go_annotations[annotation] += 1
            return set()
           
    @staticmethod
    def _extract_go_category(entry, category):
        return list({property_element.attrib['value'] for property_element in entry.xpath(r'uniprot:property[@type="%s"]' % \
                category, namespaces = UnirefToSqliteParser._NAMESPACES)})
            
    _NAMESPACE_PREFIX = r'{http://uniprot.org/uniref}'
    _NAMESPACES = {'uniprot': r'http://uniprot.org/uniref'}

    _GO_ANNOTATION_CATEGORIES = [
        'GO Molecular Function',
        'GO Biological Process',
        'GO Cellular Component',
    ]

def parse_go_annotations_meta(meta_file_path):

    ALL_FIELDS = ['id', 'name', 'namespace', 'def', 'is_a', 'synonym', 'alt_id', 'subset', 'is_obsolete', 'xref', \
            'relationship', 'intersection_of', 'disjoint_from', 'consider', 'comment', 'replaced_by', 'created_by', \
            'creation_date', 'property_value']
    LIST_FIELDS = {'synonym', 'alt_id', 'subset', 'is_a', 'xref', 'relationship', 'disjoint_from', 'intersection_of', \
            'consider', 'property_value'}
            
    GO_ANNOTATION_PATTERN = re.compile(r'\[Term\]\n((?:\w+\: .*\n?)+)')
    FIELD_LINE_PATTERN = re.compile(r'(\w+)\: (.*)')
    
    with open(meta_file_path, 'r') as f:
        raw_go_meta = f.read()

    go_annotations_meta = []

    for match in GO_ANNOTATION_PATTERN.finditer(raw_go_meta):
        
        raw_go_annotation = match.group(1)
        go_annotation = {field: [] for field in LIST_FIELDS}
        
        for line in raw_go_annotation.splitlines():
            
            (field, value), = FIELD_LINE_PATTERN.findall(line)
            assert field in ALL_FIELDS
            
            if field in LIST_FIELDS:
                go_annotation[field].append(value)
            else:
                assert field not in go_annotation
                go_annotation[field] = value
        
        go_annotations_meta.append(go_annotation)

    go_annotations_meta = pd.DataFrame(go_annotations_meta, columns = ALL_FIELDS)
    go_annotations_meta['is_obsolete'] = go_annotations_meta['is_obsolete'].fillna(False)
    assert go_annotations_meta['id'].is_unique
    go_annotations_meta.set_index('id', drop = True, inplace = True)
    go_annotations_meta.insert(0, 'index', np.arange(len(go_annotations_meta)))
    _add_children_and_parents_to_go_annotations_meta(go_annotations_meta)
    
    return go_annotations_meta
    
def _add_children_and_parents_to_go_annotations_meta(go_annotations_meta):

    go_annotations_meta['direct_children'] = [set() for _ in range(len(go_annotations_meta))]
    go_annotations_meta['direct_parents'] = [set() for _ in range(len(go_annotations_meta))]

    for go_id, go_annotation in go_annotations_meta.iterrows():
        for raw_is_a in go_annotation['is_a']:
            parent_id, parent_name = raw_is_a.split(' ! ')
            parent_go_annotation = go_annotations_meta.loc[parent_id]
            assert parent_go_annotation['name'] == parent_name
            go_annotation['direct_parents'].add(parent_id)
            parent_go_annotation['direct_children'].add(go_id)
            
    go_annotations_meta['all_ancestors'] = pd.Series(_get_index_to_all_ancestors(\
            go_annotations_meta['direct_children'].to_dict(), \
            go_annotations_meta[~go_annotations_meta['direct_parents'].apply(bool)].index))
    go_annotations_meta['all_offspring'] = pd.Series(_get_index_to_all_ancestors(\
            go_annotations_meta['direct_parents'].to_dict(), \
            go_annotations_meta[~go_annotations_meta['direct_children'].apply(bool)].index))

def _get_index_to_all_ancestors(index_to_direct_children, root_indices):
    
    index_to_all_ancestors = {index: {index} for index in index_to_direct_children.keys()}
    indices_to_scan = set(root_indices)
    
    while indices_to_scan:
        
        scanned_child_indices = set()
        
        for index in indices_to_scan:
            for child_index in index_to_direct_children[index]:
                index_to_all_ancestors[child_index].update(index_to_all_ancestors[index])
                scanned_child_indices.add(child_index)
                
        indices_to_scan = scanned_child_indices
        
    return index_to_all_ancestors
        
def _etree_fast_iter(context, func, func_args = [], func_kwargs = {}, max_elements = None):
    '''
    Based on: https://stackoverflow.com/questions/12160418/why-is-lxml-etree-iterparse-eating-up-all-my-memory
    http://lxml.de/parsing.html#modifying-the-tree
    Based on Liza Daly's fast_iter
    http://www.ibm.com/developerworks/xml/library/x-hiperfparse/
    See also http://effbot.org/zone/element-iterparse.htm
    '''
    for i, (event, elem) in enumerate(context):
        func(i, event, elem, *func_args, **func_kwargs)
        # It's safe to call clear() here because no descendants will be
        # accessed
        elem.clear()
        # Also eliminate now-empty references from the root node to elem
        for ancestor in elem.xpath('ancestor-or-self::*'):
            while ancestor.getprevious() is not None:
                del ancestor.getparent()[0]
        if max_elements is not None and i >= max_elements - 1:
            break
    del context