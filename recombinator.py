""" Simulates the process of VDJ recombination """ 
import sys
import csv
import json
import random
import numpy
import math
import os
import re
from subprocess import check_output

from Bio import SeqIO
import dendropy

from opener import opener
import utils
from event import RecombinationEvent

#----------------------------------------------------------------------------------------
class Recombinator(object):
    """ Simulates the process of VDJ recombination """
    def __init__(self, datadir, human, naivety, only_genes='', total_length_from_right=0):  # yes, with a y!
        self.tmpdir = os.getenv('PWD') + '/tmp'
        self.datadir = datadir
        self.human = human
        self.naivety = naivety
        # parameters that control recombination, erosion, and whatnot
        self.mean_n_clones = 5  # mean number of sequences to toss from each rearrangement event
        self.do_not_mutate = False
        self.only_genes = []
        self.total_length_from_right = total_length_from_right  # measured from right edge of j, only write to file this much of the sequence (our read lengths are 130 by this def'n a.t.m.)
    
        self.all_seqs = {}  # all the Vs, all the Ds...
        self.index_keys = {}  # this is kind of hackey, but I suspect indexing my huge table of freqs with a tuple is better than a dict
        self.version_freq_table = {}  # list of the probabilities with which each VDJ combo appears in data
        self.mute_models = {}
        for region in utils.regions:
            self.mute_models[region] = {}
            for model in ['gtr', 'gamma']:
                self.mute_models[region][model] = {}

        print '  init'
        # ----------------------------------------------------------------------------------------
        # first read stuff that doesn't depend on which human we're looking at
        print '    reading vdj versions'
        self.all_seqs = utils.read_germlines('.')
        print '    reading cyst and tryp positions'
        with opener('r')('data/v-meta.json') as json_file:  # get location of <begin> cysteine in each v region
            self.cyst_positions = json.load(json_file)
        with opener('r')('data/j_tryp.csv') as csv_file:  # get location of <end> tryptophan in each j region (TGG)
            tryp_reader = csv.reader(csv_file)
            self.tryp_positions = {row[0]:row[1] for row in tryp_reader}  # WARNING: this doesn't filter out the header line

        # ----------------------------------------------------------------------------------------
        # then read stuff that's specific to each human
        with opener('r')(self.datadir + '/' + self.human + '/' + self.naivety + '/gtr.txt') as gtrfile:  # read gtr parameters
            reader = csv.DictReader(gtrfile)
            for line in reader:  # these files are generated with the command: [stoat] recombinator/ > zcat data/human-beings/C/M/tree-parameters.json.gz | jq .independentParameters | grep -v '[{}]' | sed 's/["\:,]//g' | sed 's/^[ ][ ]*//' | sed 's/ /,/' | sort
                parameters = line['parameter'].split('.')
                region = parameters[0][3].lower()
                assert region == 'v' or region == 'd' or region == 'j'
                model = parameters[1].lower()
                parameter_name = parameters[2]
                assert model in self.mute_models[region]
                self.mute_models[region][model][parameter_name] = line['value']

        if only_genes != '':
            print '    restricting to: %s' % only_genes
            self.restrict_gene_choices(only_genes)
        print '    reading version freqs from %s' % (self.datadir + '/' + self.human + '/' + self.naivety + '/probs.csv.bz2')
        self.read_vdj_version_freqs(self.datadir + '/' + self.human + '/' + self.naivety + '/probs.csv.bz2')
        print '    reading tree file'
        with opener('r')(self.datadir + '/' + self.human + '/' + self.naivety + '/trees.tre') as treefile:  # read in the trees that were generated by tree-gen.r
            self.trees = treefile.readlines()

    def combine(self, outfile='', mode='overwrite'):
        """ Run the combination. """
        reco_event = RecombinationEvent(self.all_seqs)
        print 'combine'
        print '      choosing genes %45s %10s %10s %10s %10s' % (' ', 'cdr3', 'deletions', 'net', 'ok?')
        while (self.are_erosion_lengths_inconsistent(reco_event) or
               utils.is_erosion_longer_than_seq(reco_event) or
               utils.would_erode_conserved_codon(reco_event)):
            self.choose_vdj_combo(reco_event)  # set a vdj/erosion choice in reco_event
        self.get_insertion_lengths(reco_event)

        print '    chose:  gene             length'
        for region in utils.regions:
            print '        %s  %-18s %-3d' % (region, reco_event.gene_names[region], len(reco_event.seqs[region])),
            if region == 'v':
                print ' (cysteine: %d)' % reco_event.cyst_position
            elif region == 'j':
                print ' (tryptophan: %d)' % reco_event.tryp_position
            else:
                print ''

        assert not utils.is_erosion_longer_than_seq(reco_event)
        assert not utils.would_erode_conserved_codon(reco_event)
        # erode, insert, and combine
        self.erode_and_insert(reco_event)
        print '  joining'
        print '         v: %s' % reco_event.seqs['v']
        print '    insert: %s' % reco_event.insertions['vd']
        print '         d: %s' % reco_event.seqs['d']
        print '    insert: %s' % reco_event.insertions['dj']
        print '         j: %s' % reco_event.seqs['j']
        reco_event.recombined_seq = reco_event.seqs['v'] + reco_event.insertions['vd'] + reco_event.seqs['d'] + reco_event.insertions['dj'] + reco_event.seqs['j']
        reco_event.set_final_tryp_position()

        if self.do_not_mutate:
            reco_event.final_seqs.append(reco_event.recombined_seq)
        else:
            self.add_mutants(reco_event)  # toss a bunch of clones: add point mutations

        reco_event.print_event(self.total_length_from_right)

        # write some stuff that can be used by hmmer for training profiles
        # NOTE at the moment I have this *appending* to the files
#        self.write_final_vdj(reco_event)

        # write final output to csv
        if outfile != '':
            if mode == 'overwrite' and os.path.exists(outfile):
                os.remove(outfile)
            else:
                assert mode == 'append'
            print '  writing'
            reco_event.write_event(outfile, self.total_length_from_right)
        return True

    def restrict_gene_choices(self, genes):
        """ Only use the listed (colon-separated list) of genes """
        assert len(self.version_freq_table) == 0  # make sure this gets set *before* we read the freqs from file
        self.only_genes = genes.split(':')
        
    def read_vdj_version_freqs(self, fname):
        """ Read the frequencies at which various VDJ combinations appeared
        in data. This file was created with versioncounter.py
        """
        with opener('r')(fname) as infile:
            in_data = csv.DictReader(infile)
            total = 0.0  # check that the probs sum to 1.0
            for line in in_data:
                # NOTE do *not* assume the file is sorted
                if len(self.only_genes) > 0:  # are we restricting ourselves to a subset of genes?
                    if line['v_gene'] not in self.only_genes: continue
                    if line['d_gene'] not in self.only_genes: continue
                    if line['j_gene'] not in self.only_genes: continue
                total += float(line['prob'])
                index = tuple(line[column] for column in utils.index_columns)
                assert index not in self.version_freq_table
                self.version_freq_table[index] = float(line['prob'])
            if len(self.only_genes) > 0:  # renormalize if we are restricted to a subset of gene versions
                new_total = 0.0
                for index in self.version_freq_table:
                    self.version_freq_table[index] /= total
                    new_total += self.version_freq_table[index]
                assert math.fabs(new_total - 1.0) < 1e-8
            else:
                assert math.fabs(total - 1.0) < 1e-8

    def choose_vdj_combo(self, reco_event):
        """ Choose which combination germline variants to use """
        iprob = numpy.random.uniform(0,1)
        sum_prob = 0.0
        for vdj_choice in self.version_freq_table:
            sum_prob += self.version_freq_table[vdj_choice]
            if iprob < sum_prob:
                reco_event.set_vdj_combo(vdj_choice,
                                         self.cyst_positions[vdj_choice[utils.index_keys['v_gene']]]['cysteine-position'],
                                         int(self.tryp_positions[vdj_choice[utils.index_keys['j_gene']]]),
                                         self.all_seqs)
                return

        assert False  # shouldn't fall through to here

    def get_insertion_lengths(self, reco_event):
        """ Partition the necessary insertion length between the vd and dj boundaries. """
        # first get total insertion length
        total_insertion_length = reco_event.total_deletion_length + reco_event.net_length_change
        assert total_insertion_length >= 0

        # then divide total_insertion_length into vd_insertion and dj_insertion
        partition_point = numpy.random.uniform(0, total_insertion_length)
        reco_event.insertion_lengths['vd'] = int(round(partition_point))
        reco_event.insertion_lengths['dj'] = total_insertion_length - reco_event.insertion_lengths['vd']
        print '      insertion lengths: %d %d' % (reco_event.insertion_lengths['vd'], reco_event.insertion_lengths['dj'])
        assert reco_event.insertion_lengths['vd'] + reco_event.insertion_lengths['dj'] == total_insertion_length  # check for rounding problems

    def erode(self, region, location, reco_event, protected_position=-1):
        """ Erode some number of letters from reco_event.seqs[region]

        Nucleotides are removed from the <location> ('5p' or '3p') side of
        <seq>. The codon beginning at index <protected_position> is optionally
        protected from removal.
        """
        seq = reco_event.seqs[region]
        n_to_erode = reco_event.erosions[region + '_' + location]
        if protected_position > 0:  # this check is redundant at this point
            if location == '3p' and region == 'v':
                if len(seq) - n_to_erode <= protected_position + 2:
                    assert False
            elif location == '5p' and region == 'j':
                if n_to_erode - 1 >= protected_position:
                    assert False
            else:
                print 'ERROR unanticipated protection'
                sys.exit()

        fragment_before = ''
        fragment_after = ''
        if location == '5p':
            fragment_before = seq[:n_to_erode + 3] + '...'
            new_seq = seq[n_to_erode:len(seq)]
            fragment_after = new_seq[:n_to_erode + 3] + '...'
        elif location == '3p':
            fragment_before = '...' + seq[len(seq) - n_to_erode - 3 :]
            new_seq = seq[0:len(seq)-n_to_erode]
            fragment_after = '...' + new_seq[len(new_seq) - n_to_erode - 3 :]
        else:
            print 'ERROR location must be \"5p\" or \"3p\"'
            sys.exit()
        print '    %3d from %s' % (n_to_erode, location),
        print 'of %s: %15s' % (region, fragment_before),
        print ' --> %-15s' % fragment_after
        if len(fragment_after) == 0:
            print '    NOTE eroded away entire sequence'

        reco_event.seqs[region] = new_seq

    def set_insertion(self, boundary, reco_event):
        """ Set the insertions in reco_event """
        insert_seq_str = ''
        for _ in range(0, reco_event.insertion_lengths[boundary]):
            insert_seq_str += utils.int_to_nucleotide(random.randint(0, 3))

        reco_event.insertions[boundary] = insert_seq_str
        
    def erode_and_insert(self, reco_event):
        """ Erode and insert based on the lengths in reco_event. """
        print '  eroding'
        self.erode('v', '3p', reco_event, reco_event.cyst_position)
        self.erode('d', '5p', reco_event)
        self.erode('d', '3p', reco_event)
        self.erode('j', '5p', reco_event, reco_event.tryp_position)

        # then insert
        for boundary in utils.boundaries:
            self.set_insertion(boundary, reco_event)

    def write_mute_freqs(self, region, gene_name, seq, reco_event, reco_seq_fname):
        mute_freqs = {}
        # TODO this mean calculation could use some thought. Say, should it include positions that we have hardly any information for?
        mean_freq = 0.0  # calculate the mean mutation frequency. we'll use it for positions where we don't believe the actual number (eg too few alignments)
        if 'insert' in gene_name:
            mean_freq = 0.1  # TODO don't pull this number outta yo ass
        else:
            # read mutation frequencies from disk. TODO this could be cached in memory to speed things up
            mutefname = self.datadir + '/' + self.human + '/' + self.naivety + '/mute-freqs/' + utils.sanitize_name(gene_name) + '.csv'
            with opener('r')(mutefname) as mutefile:
                reader = csv.DictReader(mutefile)
                for line in reader:  # NOTE these positions are *zero* indexed
                    mute_freqs[int(line['position'])] = float(line['mute_freq'])
                    mean_freq += float(line['mute_freq'])
                mean_freq /= len(mute_freqs)
    
        # calculate mute freqs for the positions in <seq>
        rates = []
        total = 0.0
        # assert len(mute_freqs) == len(seq)  # only equal length if no erosions NO oh right but mute_freqs only covers areas we could align to...
        # TODO still, it'd be nice to have *some* way to make sure the position indices agree between mute_freqs and seq
        for inuke in range(len(seq)):  # append a freq for each nuke
            # NOTE be careful here! seqs are already eroded
            position = inuke
            if region == 'd':
                position += reco_event.erosions['d_5p']
            elif region == 'j':
                position += reco_event.erosions['j_5p']

            freq = 0.0
            if position in mute_freqs:
                freq = mute_freqs[position]
            else:  # NOTE this will happen a lot (all?) of the time for the insertions... which is ok
                freq = mean_freq

            if region == 'v' and position < 200:  # don't really have any information here
                freq = mean_freq
            # TODO add some criterion to remove positions with really large uncertainties
            rates.append(freq)
            total += freq
        if total == 0.0:  # I am not yet hip enough to divide by zero
            print 'ERROR zero total frequency in %s (probably really an insert)' % mutefname
            assert False
        for inuke in range(len(seq)):  # normalize to the number of sites
            rates[inuke] *= float(len(seq)) / total
        total = 0.0
        for inuke in range(len(seq)):  # and... double check it, just for shits and giggles
            total += rates[inuke]
        assert math.fabs(total / float(len(seq)) - 1.0) < 1e-10
        assert len(rates) == len(seq)

        # write the input file for bppseqgen, one base per line
        with opener('w')(reco_seq_fname) as reco_seq_file:
            reco_seq_file.write('state\trate\n')
            for inuke in range(len(seq)):
                reco_seq_file.write('%s\t%.15f\n' % (seq[inuke], rates[inuke]))
                
        # TODO I need to find a tool to give me the total branch length of the chosen tree, so I can compare to the number of mutations I see
        # assert False  # TODO this whole mutation frequency section kinda needs to be reread through and add a few checks

    def run_bppseqgen(self, seq, chosen_tree, gene_name, reco_event):
        """ Run bppseqgen on sequence

        Note that this is in general a piece of the full sequence (say, the V region), since
        we have different mutation models for different regions. Returns a list of mutated
        sequences.
        """
        region = 'v'  # TODO don't just use v for inserts
        if 'insert' not in gene_name :
             region = utils.get_region(gene_name)

        if len(seq) == 0:  # zero length insertion (or d)
            treg = re.compile('t[0-9][0-9]*')  # find number of leaf nodes
            n_leaf_nodes = len(treg.findall(chosen_tree))
            return ['' for _ in range(n_leaf_nodes)]  # return an empty string for each leaf node

        # write the tree to a tmp file
        treefname = self.tmpdir + '/tree.tre'
        with opener('w')(treefname) as treefile:
            treefile.write(chosen_tree)

        reco_seq_fname = self.tmpdir + '/start_seq.txt'
        self.write_mute_freqs(region, gene_name, seq, reco_event, reco_seq_fname)

        leaf_seq_fname = self.tmpdir + '/leaf-seqs.fa'
        bpp_dir = '/home/matsengrp/local/encap/bpp-master-20140414'  # on lemur: $HOME/Dropbox/work/bpp-master-20140414
        # build the command as a few separate lines
        command = 'export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:' + bpp_dir + '/lib\n'
        command += bpp_dir + '/bin/bppseqgen'  # build the bppseqgen line sequentially
        command += ' input.tree.file=' + treefname
        command += ' output.sequence.file=' + leaf_seq_fname
        command += ' number_of_sites=' + str(len(seq))
        command += ' input.tree.format=Newick'
        command += ' output.sequence.format=Fasta\(\)'
        command += ' alphabet=DNA'
        command += ' --seed=' + str(os.getpid())
        command += ' model=GTR\('
        for par in self.mute_models[region]['gtr']:
            val = self.mute_models[region]['gtr'][par]
            command += par + '=' + val + ','
        command = command.rstrip(',')
        command += '\)'
        # TODO should I use the "equilibrium frequencies" option?
        command += ' rate_distribution=\'Gamma(n=4,alpha=' + self.mute_models[region]['gamma']['alpha']+ ')\''
        command += ' input.infos.states=state'
        command += ' input.infos=' + reco_seq_fname
        command += ' input.infos.rates=rate'
        check_output(command, shell=True)

        mutated_seqs = []
        for seq_record in SeqIO.parse(leaf_seq_fname, "fasta"):  # get the leaf node sequences from the file that bppseqgen wrote
            mutated_seqs.append(str(seq_record.seq))

        # self.check_tree_simulation(leaf_seq_fname, chosen_tree)

        os.remove(reco_seq_fname)  # clean up temp files
        os.remove(treefname)
        os.remove(leaf_seq_fname)

        return mutated_seqs

    def add_mutants(self, reco_event):
        chosen_tree = self.trees[random.randint(0, len(self.trees))]
        print '  generating mutations (seed %d) with tree %s' % (os.getpid(), chosen_tree)  # TODO make sure the distribution of trees you get *here* corresponds to what you started with before you ran it through treegenerator.py
        v_mutes = self.run_bppseqgen(reco_event.seqs['v'], chosen_tree, reco_event.gene_names['v'], reco_event)
        d_mutes = self.run_bppseqgen(reco_event.seqs['d'], chosen_tree, reco_event.gene_names['d'], reco_event)
        j_mutes = self.run_bppseqgen(reco_event.seqs['j'], chosen_tree, reco_event.gene_names['j'], reco_event)
        vd_mutes = self.run_bppseqgen(reco_event.insertions['vd'], chosen_tree, 'vd_insert', reco_event)  # TODO use a better mutation model for the insertions
        dj_mutes = self.run_bppseqgen(reco_event.insertions['dj'], chosen_tree, 'dj_insert', reco_event)

        assert len(reco_event.final_seqs) == 0  # don't really need this, but it makes me feel warm and fuzzy
        for iseq in range(len(v_mutes)):
            seq = v_mutes[iseq] + vd_mutes[iseq] + d_mutes[iseq] + dj_mutes[iseq] + j_mutes[iseq]  # build final sequence
            # if mutation screwed up the conserved codons, just switch 'em back to what they were to start with
            # TODO how badly does this screw up the tree you can infer from the seqs?
            cpos = reco_event.cyst_position
            if seq[cpos : cpos + 3] != reco_event.original_cyst_word:
                seq = seq[:cpos] + reco_event.original_cyst_word + seq[cpos+3:]
            tpos = reco_event.final_tryp_position
            if seq[tpos : tpos + 3] != reco_event.original_tryp_word:
                seq = seq[:tpos] + reco_event.original_tryp_word + seq[tpos+3:]
            reco_event.final_seqs.append(seq)  # set final sequnce in reco_event

        assert not utils.are_conserved_codons_screwed_up(reco_event)
        # print '    check full seq trees'
        # self.check_tree_simulation('', chosen_tree, reco_event)

    def write_final_vdj(self, reco_event):
        """ Write the eroded and mutated v, d, and j regions to file. """
        # first do info for the whole reco event
        original_seqs = {}
        for region in utils.regions:
            original_seqs[region] = self.all_seqs[region][reco_event.gene_names[region]]
        # work out the final starting positions and lengths
        v_start = 0
        v_length = len(original_seqs['v']) - reco_event.erosions['v_3p']
        d_start = v_length + len(reco_event.insertions['vd'])
        d_length = len(original_seqs['d']) - reco_event.erosions['d_5p'] - reco_event.erosions['d_3p']
        j_start = v_length + len(reco_event.insertions['vd']) + d_length + len(reco_event.insertions['dj'])
        j_length = len(original_seqs['j']) - reco_event.erosions['j_5p']
        # then do stuff that's particular to each mutant
        for final_seq in reco_event.final_seqs:
            assert len(final_seq) == v_length + len(reco_event.insertions['vd']) + d_length + len(reco_event.insertions['dj']) + j_length
            # get the final seqs (i.e. what v, d, and j look like in the final sequence)
            final_seqs = {}
            final_seqs['v'] = final_seq[v_start:v_start+v_length]
            final_seqs['d'] = final_seq[d_start:d_start+d_length]
            final_seqs['j'] = final_seq[j_start:j_start+j_length]
            # pad with dots so it looks like (ok, is) an m.s.a. file
            final_seqs['v'] = final_seqs['v'] + reco_event.erosions['v_3p'] * '.'
            final_seqs['d'] = reco_event.erosions['d_5p'] * '.' + final_seqs['d'] + reco_event.erosions['d_3p'] * '.'
            final_seqs['j'] = reco_event.erosions['j_5p'] * '.' + final_seqs['j']
            for region in utils.regions:
                sanitized_name = reco_event.gene_names[region]  # replace special characters in gene names
                sanitized_name = sanitized_name.replace('*','_star_')
                sanitized_name = sanitized_name.replace('/','_slash_')
                out_dir = 'data/msa/' + region
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                out_fname = out_dir + '/' + sanitized_name + '.sto'
                new_line = '%15d   %s\n' % (hash(numpy.random.uniform()), final_seqs[region])
                # a few machinations such that the stockholm format has a '//' at the end
                # NOTE this method will use a lot of memory if these files get huge. I don't *anticipate* them getting huge, though
                lines = []
                if os.path.isfile(out_fname):  # if file already exists, insert the new line just before the '//'
                    with opener('r')(out_fname) as outfile:
                        lines = outfile.readlines()
                        lines.insert(-1, new_line)
                else:  # else insert everything we need
                    lines = ['# STOCKHOLM 1.0\n', new_line, '//\n']
                # did we screw this whole thing up?
                print lines
                assert lines[0].strip() == '# STOCKHOLM 1.0'
                assert lines[-1].strip() == '//'
                with opener('w')(out_fname) as outfile:
                    outfile.writelines(lines)
                    
    def are_erosion_lengths_inconsistent(self, reco_event):
        """ Are the erosion lengths inconsistent with the cdr3 length?
        TODO we need to work out why these are sometimes inconsistent (and fix it), so we're not
        just throwing out ~1/3 of the input file.
        """
        if reco_event.vdj_combo_label == ():  # haven't filled it yet
            return True
        # now are the erosion lengths we chose consistent with the cdr3_length we chose?
        total_deletion_length = 0
        for erosion in utils.erosions:
            total_deletion_length += int(reco_event.vdj_combo_label[utils.index_keys[erosion + '_del']])

        # print some crap
        gene_choices = reco_event.vdj_combo_label[utils.index_keys['v_gene']] + ' ' + reco_event.vdj_combo_label[utils.index_keys['d_gene']] + ' ' + reco_event.vdj_combo_label[utils.index_keys['j_gene']]
        print '               try: %45s %10s %10d %10d' % (gene_choices, reco_event.vdj_combo_label[utils.index_keys['cdr3_length']], total_deletion_length, reco_event.net_length_change),
        is_bad = (-total_deletion_length > reco_event.net_length_change)
        if is_bad:
            print '%10s' % 'no'
        else:
            print '%10s' % 'yes'

#        # write out some stuff for connor to check
#        outfname = 'for-connor.csv'
#        if os.path.isfile(outfname):
#            mode = 'ab'
#        else:
#            mode = 'wb'
#        columns = ('v_gene', 'd_gene', 'j_gene', 'cdr3_length', 'initial_cdr3_length', 'v_3p_del', 'd_5p_del', 'd_3p_del', 'j_5p_del', 'is_bad')
#        with opener('ab')(outfname) as outfile:
#            writer = csv.DictWriter(outfile, columns)
#            if mode == 'wb':  # write the header if file wasn't there before
#                writer.writeheader()
#            # fill the row with values
#            row = {}
#            # first the stuff that's common to the whole recombination event
#            row['cdr3_length'] = reco_event.cdr3_length
#            row['initial_cdr3_length'] = reco_event.current_cdr3_length
#            for region in utils.regions:
#                row[region + '_gene'] = reco_event.gene_names[region]
#            for erosion_location in utils.erosions:
#                row[erosion_location + '_del'] = reco_event.erosions[erosion_location]
#            row['is_bad'] = is_bad
#            writer.writerow(row)

        # i.e. we're *in*consistent if net change is negative and also less than total deletions
        return is_bad

    def check_tree_simulation(self, leaf_seq_fname, chosen_tree_str, reco_event=None):
        """ See how well we can reconstruct the true tree """
        clean_up = False
        if leaf_seq_fname == '':  # we need to make the leaf seq file based on info in reco_event
            clean_up = True
            leaf_seq_fname = self.tmpdir + '/leaf-seqs.fa'
            with opener('w')(leaf_seq_fname) as leafseqfile:
                for iseq in range(len(reco_event.final_seqs)):
                    leafseqfile.write('>t' + str(iseq+1) + '\n')  # TODO the *order* of the seqs doesn't correspond to the tN number. does it matter?
                    leafseqfile.write(reco_event.final_seqs[iseq] + '\n')

        with opener('w')(os.devnull) as fnull:
            inferred_tree_str = check_output('FastTree -gtr -nt ' + leaf_seq_fname, shell=True, stderr=fnull)
        if clean_up:
            os.remove(leaf_seq_fname)
        chosen_tree = dendropy.Tree.get_from_string(chosen_tree_str, 'newick')
        inferred_tree = dendropy.Tree.get_from_string(inferred_tree_str, 'newick')
        print '        tree diff -- symmetric %d   euke %f   rf %f' % (chosen_tree.symmetric_difference(inferred_tree), chosen_tree.euclidean_distance(inferred_tree), chosen_tree.robinson_foulds_distance(inferred_tree))
