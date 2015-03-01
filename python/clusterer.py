import sys
import csv
import math
from operator import itemgetter
from subprocess import check_call

import utils
import plotting
from opener import opener
# ./venv/bin/linsim compare-clustering --true-name-column unique_id --inferred-name-column unique_id  --true-group-column reco_id --inferred-group-column reco_id /tmp/dralph/true.csv /tmp/dralph/inf.csv

class Clusterer(object):
    # ----------------------------------------------------------------------------------------
    def __init__(self, threshold=0.0, greater_than=True, singletons=[]):  # put in same cluster if greater than threshold, or less than equal to?
        # self.method = method
        # if method == 'single-link':
        #     pass
        # elif method == 'hierarch-agglom':
        #     pass
        # else:
        #     raise Exception('ERROR bad clustering method ' + method)
        self.threshold = threshold
        self.debug = False
        self.greater_than = greater_than
        self.max_id = -1  # maximum previously used id
        self.cluster_ids = []
        self.query_clusters = {}  # map from query name to cluster id
        self.id_clusters = {}  # map from cluster id to list of query names
        for st in singletons:
            self.add_new_cluster(st, dbg_str_list=[])
        self.singletons = singletons
        self.pairscores = {}  # used by external code to see if we saw a given pair
        self.plotscores = { 'all':[], 'same':[], 'diff':[]}  # keep track of scores for plotting

        # self.nearest_true_mate = {}  #

    # ----------------------------------------------------------------------------------------
    def glomerate(self, log_probs):
        sorted_log_probs = sorted(log_probs.items(), key=itemgetter(1))
        for stuff in sorted_log_probs:
            print stuff[1], stuff[0]

    # ----------------------------------------------------------------------------------------
    def hierarch_agglom(self, log_probs=None, partitions=None, infname=None, debug=False, reco_info=None, outfile=None, plotdir=''):
        # """ If we get <log_probs> but not <partitions>, do hierarchical agglomeration from scratch
        # self.glomerate(log_probs)
        self.max_log_prob, self.best_partition = None, None
        for part in partitions:  # NOTE these are sorted in order of agglomeration, with the initial partition first
            print '  %-8.3f' % part['score'],
            for cluster in part['clusters']:
                print ':'.join([ str(uid) for uid in cluster]),
            print ''

            if self.max_log_prob is None or part['score'] > self.max_log_prob:
                self.max_log_prob = part['score']
                self.best_partition = part['clusters']

        print 'best partition ', self.max_log_prob
        for cluster in self.best_partition:
            print '   ', ':'.join([ str(uid) for uid in cluster ])

        self.max_minus_ten_log_prob, self.best_minus_ten_partition = None, None  # reel back glomeration by ten units of log prob to be conservative before we pass to the multiple-process merge
        for part in partitions:
            if part['score'] > self.max_log_prob - 10.0:
                self.max_minus_ten_log_prob = part['score']
                self.best_minus_ten_partition = part['clusters']
                break
                
        print 'best minus ten ', self.max_minus_ten_log_prob
        for cluster in self.best_minus_ten_partition:
            print '   ', ':'.join([ str(uid) for uid in cluster ])

    # ----------------------------------------------------------------------------------------
    def single_link(self, input_scores=None, infname=None, debug=False, reco_info=None, outfile=None, plotdir=''):
        if infname is None:
            assert input_scores is not None
        else:
            assert input_scores is None  # should only specify <input_scores> *or* <infname>
            input_scores = []
            with opener('r')(infname) as infile:
                reader = csv.DictReader(infile)
                for line in reader:
                    input_scores.append(line)
        sorted_lines = sorted(input_scores, key=lambda k: float(k['score']))
        for line in sorted_lines:
            a_name = line['id_a']
            b_name = line['id_b']
            score = float(line['score'])
            from_same_event = -1 if (reco_info == None or a_name not in reco_info or b_name not in reco_info) else reco_info[a_name]['reco_id'] == reco_info[b_name]['reco_id']
            dbg_str_list = ['%22s %22s   %8.3f   %d' % (a_name, b_name, score, from_same_event), ]
            self.incorporate_into_clusters(a_name, b_name, score, dbg_str_list)
            self.pairscores[(utils.get_key((a_name, b_name)))] = score
            self.plotscores['all'].append(score)
            if reco_info != None:
                if from_same_event:
                    self.plotscores['same'].append(score)
                else:
                    self.plotscores['diff'].append(score)
            # if reco_info != None and reco_info[a_name]['reco_id'] == reco_info[b_name]['reco_id']:
            #     for query,score in {a_name:score, b_name:score}.iteritems():
            #         if query not in self.nearest_true_mate:
            #             self.nearest_true_mate[query] = score
            #         elif self.greater_than and score > self.nearest_true_mate[query]:
            #             self.nearest_true_mate[query] = score
            #         elif not self.greater_than and score < self.nearest_true_mate[query]:
            #             self.nearest_true_mate[query] = score
            if debug:
                outstr = ''.join(dbg_str_list)
                if outfile == None:
                    print outstr
                else:
                    outfile.write(outstr + '\n')

        if plotdir != '':
            utils.prep_dir(plotdir + '/plots', '*.svg')
            hists = {}
            for htype in ['all', 'same', 'diff']:
                hists[htype] = plotting.make_hist_from_list(self.plotscores[htype], htype + '_pairscores')
                hists[htype].SetTitle(htype)
            plotting.draw(hists['all'], 'float', plotdir=plotdir, plotname='pairscores', more_hists=[hists['same'], hists['diff']])
            check_call(['./bin/makeHtml', plotdir, '3', 'null', 'svg'])
            check_call(['./bin/permissify-www', plotdir])

        for query, cluster_id in self.query_clusters.iteritems():
            if cluster_id not in self.id_clusters:
                self.id_clusters[cluster_id] = []
            self.id_clusters[cluster_id].append(query)
        for cluster_id, queries in self.id_clusters.items():
            if len(queries) == 1:
                self.singletons.append(queries[0])

        # print 'nearest',self.nearest_true_mate
        out_str_list = ['  %d clusters:\n'%len(self.id_clusters), ]
        for cluster_id in self.id_clusters:
            out_str_list.append('   ' + ' '.join([str(x) for x in self.id_clusters[cluster_id]]) + '\n')
        if outfile == None:
            print ''.join(out_str_list)
        else:
            outfile.write(''.join(out_str_list))

    # ----------------------------------------------------------------------------------------
    def add_new_cluster(self, query_name, dbg_str_list):
        dbg_str_list.append('    new cluster ' + str(query_name))
        assert query_name not in self.query_clusters
        self.max_id += 1
        self.query_clusters[query_name] = self.max_id
        self.cluster_ids.append(self.max_id)

    # ----------------------------------------------------------------------------------------
    def merge_clusters(self, query_name, second_query_name, dbg_str_list):
        """ move all queries with same id as <second_query_name> to <query_name>'s cluster """
        if self.query_clusters[query_name] == self.query_clusters[second_query_name]:
            dbg_str_list.append('     already together')
            return
        dbg_str_list.append('     merging ' + str(self.query_clusters[query_name]) + ' and ' + str(self.query_clusters[second_query_name]))
        first_cluster_id = self.query_clusters[query_name]
        second_cluster_id = self.query_clusters[second_query_name]

        if first_cluster_id == second_cluster_id:  # already in the same cluster
            return
        for name,cluster_id in self.query_clusters.iteritems():
            if cluster_id == second_cluster_id:
                self.query_clusters[name] = first_cluster_id

        if second_cluster_id in self.cluster_ids:
            self.cluster_ids.remove(second_cluster_id)
        else:
            print 'oh, man, something\'s wrong'
            print 'uniqe_id,reco_id'
            for name,cluster_id in self.query_clusters.iteritems():
                print '%s,%d' % (name, cluster_id)
            sys.exit()

    # ----------------------------------------------------------------------------------------
    def add_to_cluster(self, cluster_id, query_name, dbg_str_list):
        dbg_str_list.append('    adding ' + str(query_name) + ' to ' + str(cluster_id))
        self.query_clusters[query_name] = cluster_id

    # ----------------------------------------------------------------------------------------
    def is_removable(self, score):
        if math.isnan(score):
            assert False
        if self.greater_than:
            return score <= self.threshold
        else:
            return score > self.threshold

    # ----------------------------------------------------------------------------------------
    def incorporate_into_clusters(self, query_name, second_query_name, score, dbg_str_list):
        """ figure out how to add query pair into clusters using single-link"""
        if math.isnan(score):
            print 'ERROR nan passed for %d %d (dbg %s)' %(query_name, second_query_name, dbg_str_list)
            sys.exit()
        if self.is_removable(score):
            dbg_str_list.append('    removing link')
            if query_name not in self.query_clusters:
                self.add_new_cluster(query_name, dbg_str_list)
            if second_query_name not in self.query_clusters:
                self.add_new_cluster(second_query_name, dbg_str_list)
            return
        if query_name in self.query_clusters and second_query_name in self.query_clusters:  # if both seqs are already in clusters
            self.merge_clusters(query_name, second_query_name, dbg_str_list)
        elif query_name in self.query_clusters:
            self.add_to_cluster(self.query_clusters[query_name], second_query_name, dbg_str_list)
        elif second_query_name in self.query_clusters:
            self.add_to_cluster(self.query_clusters[second_query_name], query_name, dbg_str_list)
        else:
            self.add_new_cluster(query_name, dbg_str_list)
            self.add_to_cluster(self.query_clusters[query_name], second_query_name, dbg_str_list)
