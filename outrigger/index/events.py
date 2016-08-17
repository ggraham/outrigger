import itertools
import logging

import graphlite
from graphlite import V
import numpy as np
import pandas as pd

from .region import Region
from .adjacencies import UPSTREAM, DOWNSTREAM, DIRECTIONS

from ..io.common import STRAND
from ..util import progress


EVENT_TYPES = (('skipped_exon', 'se'), ('mutually_exclusive_exon', 'mxe'))

ISOFORM_ORDER = 'isoform1', 'isoform2'
ISOFORM_COMPONENTS = {'se': {'isoform1': ('junction13',),
                             'isoform2': ('junction12', 'exon2', 'junction23')
                             },
                      'mxe': {'isoform1': ('junction13', 'exon3',
                                           'junction34'),
                             'isoform2': ('junction12', 'exon2', 'junction24')
                             }
                      }
EVENT_ID_COLUMN = 'event_id'

def stringify_location(chrom, start, stop, strand, region=None):
    """"""
    if region is not None:
        return '{0}:{1}:{2}-{3}:{4}'.format(region, chrom, start, stop,
                                            strand)
    else:
        return '{0}:{1}-{2}:{3}'.format(chrom, start, stop, strand)


def opposite(direction):
    return UPSTREAM if direction == DOWNSTREAM else DOWNSTREAM


class EventMaker(object):

    def __init__(self, junction_exon_triples, db=None, junction_col='junction',
                 exon_col='exon'):
        """Combine splice junctions into splicing events

        Parameters
        ----------
        junction_exon_triples : pandas.DataFrame
            of "exon, direction, junction", e.g.:
            exon1, upstream, junction12

        db : gffutils.FeatureDB
            Gffutils Database of gene, transcript, and exon features. The exons
            must be accessible by the id provided on the `exon_col`
            columns. If not provided, certain splice types which require
            information about the transcript (AFE, ALE) cannot be annotated.
        """
        self.log = logging.getLogger('EventMaker')
        self.junction_exon_triples = junction_exon_triples
        self.db = db

        self.graph = graphlite.connect(":memory:", graphs=DIRECTIONS)
        self.exons = tuple(junction_exon_triples[exon_col].unique())
        self.n_exons = len(self.exons)
        self.junctions = tuple(junction_exon_triples[junction_col].unique())

        self.items = tuple(np.concatenate([self.exons, self.junctions]))
        self.item_to_region = pd.Series(map(Region, self.items),
                                        index=self.items)

        with self.graph.transaction() as tr:
            for i, row in self.junction_exon_triples.iterrows():
                junction = row[junction_col]
                exon = row[exon_col]

                junction_i = self.items.index(junction)
                exon_i = self.items.index(exon)

                self.log.debug('\n{} is {} of {}\n'.format(
                    exon, row.direction, junction))
                self.log.debug('{} is {} of {}\n'.format(
                    junction, opposite(row.direction), exon))

                tr.store(getattr(V(exon_i), row.direction)(junction_i))
                tr.store(getattr(V(junction_i),
                                 opposite(row.direction))(exon_i))

    @property
    def exon_progress_interval(self):
        return int(np.ceil(self.n_exons / 100.))

    def _maybe_print_exon_progress(self, i):
        if (i + 1) % self.exon_progress_interval == 0:
            progress('\t{0}/{1} exons tested ({2:.1f}%)'.format(
                i + 1, self.n_exons, 100 * (i + 1) / float(self.n_exons)))

    def event_dict_to_df(self, events, exon_names, junction_names):
        columns = list(exon_names) + list(junction_names) \
                  + ['exons', 'junctions']
        data = pd.DataFrame(index=np.arange(len(events)), columns=columns)
        for i, (exons, junctions) in enumerate(events.items()):
            exon_ids = '@'.join(exons)
            junction_ids = '@'.join(junctions)
            data.loc[i, exon_names] = list(exons)
            data.loc[i, junction_names] = list(junctions)
            data.loc[i, 'exons'] = exon_ids
            data.loc[i, 'junctions'] = junction_ids
            data.loc[i, STRAND] = exons[0][-1]
        return data

    def add_event_id_col(self, events, splice_type):
        isoform_components = ISOFORM_COMPONENTS[splice_type]
        events[EVENT_ID_COLUMN] = events.apply(
            lambda x: '|'.join('{}={}'.format(isoform,
                                      '@'.join(isoform_components[isoform]))
                       for isoform in ISOFORM_ORDER), axis=1)
        return events

    def exons_one_junction_downstream(self, exon_i):
        """Get the exon(s) that are immediately downstream of this one

        Get exons that are downstream from this one, separated by one junction

        Parameters
        ----------
        exon_i : int
            Integer identifier of the exon whose downstream exons you want.
            This is the exon's index location in self.exons

        Returns
        -------
        downstream_exons : graphlite.Query
            Integer identfiers of exons which are one junction downstream
            of the provided one
        """
        return self.graph.find(
                V().downstream(exon_i)).traverse(V().upstream)

    def exons_one_junction_upstream(self, exon_query):
        """Get the exon(s) that are immediately upstream of this one

        Get exons that are upstream from this one, separated by one junction

        Parameters
        ----------
        exon_query : graphlite.Query
            Integer identifier of the exon whose upstream exons you want.
            This is the exon's index location in self.exons

        Returns
        -------
        upstream_exons : graphlite.Query
            Integer identfiers of exons which are one junction upstream
            of the provided one
        """
        return exon_query.traverse(V().downstream).traverse(
            V().downstream)

    def exons_two_junctions_downstream(self, exon_i):
        """Get the exon(s) that are two junction hops downstream

        Go one exon downstream, then one more exon. This is all the 2nd level
        exons

        Parameters
        ----------
        exon_i : int
            Integer identifier of the exon whose downstream exons you want.
            This is the exon's index location in self.exons

        Returns
        -------
        downstream_exons : graphlite.Query
            Integer identfiers of exons which are separated from the original
            exon by a junction, exon, and another junction
        """
        return self.graph.find(V().downstream(exon_i)).traverse(
            V().upstream).traverse(V().upstream).traverse(V().upstream)

    def junctions_between_exons(self, exon_a, exon_b):
        return self.graph.find(
            V(exon_a).upstream) \
            .intersection(V(exon_b).downstream)

    def skipped_exon(self):
        events = {}

        progress('Trying out {0} exons ...'.format(self.n_exons))
        for exon1_i, exon1_name in enumerate(self.exons):
            self._maybe_print_exon_progress(exon1_i)

            exon23s = list(self.exons_one_junction_downstream(exon1_i))
            exon23s = self.item_to_region[[self.items[i] for i in exon23s]]

            for exon_a, exon_b in itertools.combinations(exon23s, 2):
                if not exon_a.overlaps(exon_b):
                    exon2 = min((exon_a, exon_b), key=lambda x: x._start)
                    exon3 = max((exon_a, exon_b), key=lambda x: x._start)

                    exon2_i = self.exons.index(exon2.name)
                    exon3_i = self.exons.index(exon3.name)

                    exon23_junction = list(self.graph.find(
                        V(exon2_i).upstream).intersection(
                        V().upstream(exon3_i)))
                    if len(exon23_junction) > 0:
                        # Isoform 1 - corresponds to Psi=0. Exclusion of exon2
                        exon13_junction = self.junctions_between_exons(
                            exon1_i, exon3_i)

                        # Isoform 2 - corresponds to Psi=1. Inclusion of exon2
                        exon12_junction = self.junctions_between_exons(
                            exon1_i, exon2_i)

                        junctions_i = list(itertools.chain(
                            *[exon12_junction, exon23_junction,
                              exon13_junction]))
                        junctions = [self.items[i] for i in junctions_i]
                        exons = exon1_name, exon2.name, exon3.name

                        events[exons] = junctions
        events = self.event_dict_to_df(
            events, exon_names=['exon1', 'exon2', 'exon3'],
            junction_names=['junction12', 'junction23', 'junction13'])
        events = self.add_event_id_col(events, 'se')
        return events

    def mutually_exclusive_exon(self):
        events = {}

        progress('Trying out {0} exons ...'.format(self.n_exons))
        for i, exon1_name in enumerate(self.exons):
            self._maybe_print_exon_progress(i)

            exon1_i = self.items.index(exon1_name)

            exon23s_from1 = self.exons_one_junction_downstream(exon1_i)
            exon4s = self.exons_two_junctions_downstream(exon1_i)
            exon23s_from4 = self.exons_one_junction_upstream(exon4s)

            exon23s = set(exon23s_from4) & set(exon23s_from1)
            exon23s = [self.items[i] for i in exon23s]

            exon23s = self.item_to_region[exon23s]

            for exon_a, exon_b in itertools.combinations(exon23s, 2):
                if not exon_a.overlaps(exon_b):
                    exon2 = min((exon_a, exon_b), key=lambda x: x._start)
                    exon3 = max((exon_a, exon_b), key=lambda x: x._start)

                    exon2_i = self.items.index(exon2.name)
                    exon3_i = self.items.index(exon3.name)

                    exon4_from2 = set(
                        self.exons_one_junction_downstream(exon2_i))
                    exon4_from3 = set(
                        self.exons_one_junction_downstream(exon3_i))

                    try:
                        exon4_i = (exon4_from2 & exon4_from3).pop()
                        exon4_name = self.items[exon4_i]
                        # Isoform 1 - corresponds to Psi=0. Inclusion of exon3
                        exon13_junction = self.junctions_between_exons(
                            exon1_i, exon3_i)

                        exon34_junction = self.junctions_between_exons(
                            exon3_i, exon4_i)

                        # Isoform 2 - corresponds to Psi=1. Inclusion of exon2
                        exon12_junction = self.junctions_between_exons(
                            exon1_i, exon2_i)
                        exon24_junction = self.junctions_between_exons(
                            exon2_i, exon4_i)

                        exon_tuple = exon1_name, exon2.name, exon3.name, \
                            exon4_name
                        #             print exon12_junction.next()
                        junctions_i = list(
                            itertools.chain(*[exon13_junction,
                                              exon34_junction,
                                              exon12_junction,
                                              exon24_junction]))
                        junctions = [self.items[i] for i in junctions_i]

                        events[exon_tuple] = junctions
                    except KeyError:
                        pass
        events = self.event_dict_to_df(events,
                                       exon_names=['exon1', 'exon2', 'exon3',
                                                   'exon4'],
                                       junction_names=['junction13',
                                                       'junction34',
                                                       'junction12',
                                                       'junction24'])
        events = self.add_event_id_col(events, 'mxe')
        return events