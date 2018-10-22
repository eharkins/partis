#!/usr/bin/env python
import os
import sys
import csv
import argparse
import operator
import argparse
import yaml
import colored_traceback.always

# if you move this script, you'll need to change this method of getting the imports
partis_dir = os.path.dirname(os.path.realpath(__file__)).replace('/bin', '')
sys.path.insert(1, partis_dir + '/python')

import utils

parser = argparse.ArgumentParser()
parser.add_argument('infname')
parser.add_argument('--config-fname', help='yaml file with info on columns for which we want to specify particular values (and skip others). Default/example set below.')
parser.add_argument('--outfname')
parser.add_argument('--debug', action='store_true')
args = parser.parse_args()

if args.config_fname is None:
    non_summed_column = 'v_gene'
    skip_column_vals = {  # to input your own dict on the command line, just convert with str() and quote it
        # 'cdr3_length' : ['33', '36', '39', '42', '45', '48'],  # <value> is list of acceptable values NOTE need to all be strings, otherwise you have to worry about converting the values in the csv file

        # bf520.1:
        'v_gene' : ['IGHV1-2*02+G35A', 'IGHV1-2*02+T147C', 'IGHV1-2*02'],
        # 'd_gene' : ['IGHD3-22*01'],
        'j_gene' : ['IGHJ4*02'],
        'cdr3_length' : ['66',],  #  TGTGCGAGAGGGCCATTCCCGAATTACTATGGTCCGGGGAGTTATTGGGGGGGTTTTGACCACTGG
    }
else:
    with open(args.config_fname) as yamlfile:
        yamlfo = yaml.load(yamlfile)
        non_summed_column = yamlfo['non_summed_column']
        skip_column_vals = yamlfo['skip_column_vals']

info = {}
lines_skipped, lines_used = 0, 0
counts_skipped, counts_used = 0, 0
with open(args.infname) as csvfile:
    reader = csv.DictReader(csvfile)
    # if args.debug:
    #     print '  all columns in file: %s' % ' '.join(reader.fieldnames)
    if len(set(skip_column_vals) - set(reader.fieldnames)) > 0:
        raise Exception('keys in --skip-column-fname not in file: %s' % ' '.join(set(skip_column_vals) - set(reader.fieldnames)))
    for line in reader:
        skip_this_line = False
        for scol, acceptable_values in skip_column_vals.items():
            if line[scol] not in acceptable_values:
                skip_this_line = True
                lines_skipped += 1
                counts_skipped += int(line['count'])
                break
        if skip_this_line:
            continue

        if line[non_summed_column] not in info:
            info[line[non_summed_column]] = 0
        info[line[non_summed_column]] += int(line['count'])
        lines_used += 1
        counts_used += int(line['count'])

if args.debug:
    import fraction_uncertainty
    def frac_err(obs, total):
        lo, hi = fraction_uncertainty.err(obs, total)
        return 0.5 * (hi - lo)

    print '  applied restrictions:'
    for scol, acceptable_values in skip_column_vals.items():
        print '      %15s in %s' % (scol, acceptable_values)
    print '   used:'
    print '     %6d / %-6d = %.3f  lines'  % (lines_used, lines_used + lines_skipped, lines_used / float(lines_used + lines_skipped))
    print '     %6d / %-6d = %.3f +/- %.3f counts'  % (counts_used, counts_used + counts_skipped, counts_used / float(counts_used + counts_skipped), frac_err(counts_used, counts_used + counts_skipped))

    print '     %s    count      / %d = fraction' % (non_summed_column, counts_used)
    # for val, count in sorted(info.items(), key=operator.itemgetter(1), reverse=True):  # sort by counts
    for val, count in sorted(info.items()):  # sort by column value (e.g. cdr3 length)
        print '   %12s   %6d          %.3f +/- %.3f' % (val, count, count / float(counts_used), frac_err(count, counts_used))

if args.outfname is not None:
    if args.debug:
        print '  writing %d info entries to %s' % (len(info), args.outfname)
    with open(args.outfname, 'w') as outfile:
        yamlfo = {'counts' : counts_used,
                  'total' : counts_used + counts_skipped,
                  'info' : info}
        yaml.dump(yamlfo, outfile, width=150)
