#!/usr/bin/env python
"""
Script to run combine to fit R(D*).

To activate the environment run:

    $ cd ~/RDstAnalysis/CMSSW_10_2_13/src/
    $ cmsenv
    $ cd ~/RDstAnalysis/BPH_RD_Analysis/Combine/

To run multiple categories via condor at once:

    $ for cat in low mid high comb; do ./runCombine.py -v [version] --cat $cat --submit; done
"""
import sys, os, pickle, time, json, yaml, itertools, commands, re
from datetime import datetime
from glob import glob
from collections import defaultdict
from multiprocessing import Pool
from prettytable import PrettyTable
import numpy as np
import pandas as pd
from scipy.stats import chi2 as scipy_chi2
import matplotlib.pyplot as plt
from array import array
import subprocess, psutil
import ROOT as rt
rt.PyConfig.IgnoreCommandLineOptions = True
rt.gErrorIgnoreLevel = rt.kError
rt.RooMsgService.instance().setGlobalKillBelow(rt.RooFit.ERROR)
import root_numpy as rtnp
import collections
from os.path import join, dirname

try:
    from categoriesDef import categories as categoriesDef
    from analysis_utilities import drawOnCMSCanvas, getEff, DSetLoader, str2bool, load_data, NTUPLE_TAG, load_yaml, print_warning, TRIGGER_SCALE_FACTOR, BD_CALIBRATION
    from beamSpot_calibration import getBeamSpotCorrectionWeights
    from pT_calibration_reader import pTCalReader as kinCalReader
    from histo_utilities import create_TH1D, create_TH2D, std_color_list, make_ratio_plot
    from gridVarQ2Plot import plot_gridVarQ2, plot_SingleCategory, getControlXtitle, getControlSideText
    from lumi_utilities import getLumiByTrigger
    from combine_utilities import getUncertaintyFromLimitTree, dumpDiffNuisances, stringJubCustomizationCaltechT2, loadHisto4CombineFromRoot, getResultsFromMultiDimFitSingles
except ImportError:
    print "Failed to import analysis_utilities."
    print "Did you remember to source the env.sh file in the repo?"
    print >> sys.stderr, "Failed to import analysis_utilities."
    print >> sys.stderr, "Did you remember to source the env.sh file in the repo?"
    sys.exit(1)

from progressBar import ProgressBar

import CMS_lumi, tdrstyle
tdrstyle.setTDRStyle()
CMS_lumi.writeExtraText = 1
CMS_lumi.extraText = "     Preliminary"
donotdelete = []

data_over_MC_overallNorm = 0.83

# The tuples have the following columns:
#     0. procId (set in B2DstMu_skimCAND_v1.py)
#     1. central value (relative to Monte Carlo cards)
#     2. relative uncertainty
#     3. multiplication factor for relative uncertainty
uncertainties_DstPi_mix = np.genfromtxt('uncertainties_DstPi_processes.txt', dtype=None)
uncertainties_DstPiPi_mix = np.genfromtxt('uncertainties_DstPiPi_processes.txt', dtype=None)
uncertainties_DstHc_mix = np.genfromtxt('uncertainties_DstHc_processes.txt', dtype=None)
DstHc_sample_id = {'Bd_DstDu':1, 'Bd_DstDd':2, 'Bd_DstDs': 3, 'Bu_DstDu':4, 'Bu_DstDd':5, 'Bs_DstDs':6}

categoriesToCombine = ['low', 'mid', 'high']

binning = {'q2': array('d', [0, 3.5, 6, 9.4, 12])}

SM_RDst = 0.295
expectedLumi = {'Low':6.4, 'Mid':20.7, 'High':26.4, 'Single':20.7} #fb^-1

FreeParFF = {
   'CLN': ['R0', 'eig1', 'eig2', 'eig3'],
   'BLPR': ['eig1', 'eig2', 'eig3', 'eig4', 'eig5', 'eig6'],
   'NoFF': []
}

processOrder = [
    'tau', 'mu',
    'Bu_MuDstPi', 'Bd_MuDstPi',
    'Bd_MuDstPiPi', 'Bu_MuDstPiPi',
    'Bu_TauDstPi', 'Bd_TauDstPi',
    'Bd_TauDstPiPi', 'Bu_TauDstPiPi',
    'Bs_MuDstK', 'Bs_TauDstK',
    'Bd_DstDu', 'Bu_DstDu',
    'Bd_DstDd', 'Bu_DstDd',
    'Bd_DstDs', 'Bs_DstDs',
    'Bd_DDs1', 'Bu_DDs1',
    #'B_DstDXX',
    'dataSS_DstMu',
    'Bu_D2stDs'
]

samples_Bd = [p  for p in processOrder if (p[:2] == 'Bd' or p in ['tau', 'mu'])]
samples_Bu = [p  for p in processOrder if p[:2] == 'Bu']
samples_Bs = [p  for p in processOrder if p[:2] == 'Bs']

controlRegSel = {}
def selfun__TkPlus(ds):
    sel = ds['ctrl2'] == 1
    return sel
controlRegSel['p_'] = selfun__TkPlus

def selfun__TkMinus(ds):
    sel = ds['ctrl2'] == 2
    return sel
controlRegSel['m_'] = selfun__TkMinus

def selfun__TkPlusMinus(ds):
    sel = (ds['ctrl2'] == 12) | (ds['ctrl2'] == 21)
    sel = np.logical_and(ds['massVisTks'] < 5.55, sel)
    return sel
controlRegSel['pm'] = selfun__TkPlusMinus

def selfun__TkMinusMinus(ds):
    sel = ds['ctrl2'] == 22
    sel = np.logical_and(ds['massVisTks'] < 5.3, sel)
    return sel
controlRegSel['mm'] = selfun__TkMinusMinus

def selfun__TkPlusPlus(ds):
    sel = ds['ctrl2'] == 11
    sel = np.logical_and(ds['massVisTks'] < 5.3, sel)
    return sel
controlRegSel['pp'] = selfun__TkPlusPlus

# Check for the right singularity using:
#
#     $ ll /cvmfs/singularity.opensciencegrid.org/cmssw/
basedir = dirname(dirname(os.path.abspath(__file__)))

CONDOR_TEMPLATE = \
"""
executable        = {basedir}/Combine/condorJob.sh
environment       = {environment}
arguments         = {arguments}
output            = {outdir}/job_{jN}_$(ClusterId).out
error             = {outdir}/job_{jN}_$(ClusterId).err
log               = {outdir}/job_{jN}_$(ClusterId).log
JobPrio           = -1
WHEN_TO_TRANSFER_OUTPUT = ON_EXIT_OR_EVICT
+MaxRuntime       = 1200
+JobQueue         = {jobqueue}
+RunAsOwner       = True
+InteractiveUser  = True
+SingularityImage = "/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel7"
+SingularityBindCVMFS = True
run_as_owner      = True
RequestDisk       = 15000000
RequestMemory     = {mem}
RequestCpus       = {cpus}
x509userproxy     = $ENV(X509_USER_PROXY)
on_exit_remove    = (ExitBySignal == False) && (ExitCode == 0)
on_exit_hold      = (ExitBySignal == True) || (ExitCode != 0)
+PeriodicRemove   = ((JobStatus =?= 2) && ((MemoryUsage =!= UNDEFINED && MemoryUsage > 5*RequestMemory)))
requirements      = Machine =!= LastRemoteHost
universe          = vanilla
queue 1
"""

def update(d, u):
    # From https://stackoverflow.com/questions/3232943/update-value-of-a-nested-dictionary-of-varying-depth
    for k, v in u.iteritems():
        if isinstance(v, collections.Mapping):
            d[k] = update(d.get(k, {}), v)
        else:
            d[k] = v
    return d

PV_QUANTITIES = ['q2','Est_mu','M2_miss']

def histogram_ctrl(ds, name, bins, weightsCentral, wVar, sel, ctrlVar, ctrlVar_mod, scale):
    """
    Returns a dictionary containing all the control region histograms needed
    for the fit.

    Arguments:
        - ds: pandas dataframe
            Dataframe containing the event info.
        - name: str
            Name of the process.
        - bins: dict
            Dictionary describing the binning in various observables.
        - weightsCentral: np.array
            central weights
        - wVar: dict
            Dictionary of weights.
        - sel: dict
            Dictionary of arrays to select each region.
        - ctrlVar: dict
        - ctrlVar_mod: dict
        - scale: dict
            dictionary of scale factors for each region
    """
    # Add systematic uncertainty terms for PV_x, PV_y, and PV_z. These don't
    # have any special weight, but we instead use q2, Est_mu, and M2_miss with
    # the shifted primary vertex when histogramming the data
    wVar_new = {k: v for k, v in wVar.iteritems()}
    for attr in ['x','y','z']:
        for wVar in ['Up','Down']:
            wVar_new['PV_%s%s' % (attr,wVar)] = np.ones_like(weightsCentral)

    histo = {}
    for name_wVar, v_wVar in wVar_new.iteritems():
        h_name = name
        if name_wVar != '':
            h_name += '__' + name_wVar
        w = weightsCentral*v_wVar

        for k, var in ctrlVar.iteritems():
            if k not in histo:
                histo[k] = {}

            region = k[5:7]

            if 'data' not in name and name_wVar.startswith('PV_') and var in PV_QUANTITIES:
                var += name_wVar[2:]

            auxSel = sel[region]
            if ctrlVar_mod[k] is not None:
                m, j = ctrlVar_mod[k]
                if m > 1:
                    auxSel = np.logical_and(np.mod(ds['index'], m) == j, auxSel)

            histo[k][h_name] = create_TH1D(ds[var][auxSel], name=h_name, title=h_name, binning=bins[k], opt='', weights=w[auxSel], scale_histo=scale[region])

    return histo

def histogram(ds, name, bins, bins_2D, weightsCentral, wVar, observables_q2bins, observables_q2integrated, nTotExp, nTotSelected):
    """
    Returns a dictionary containing all the signal region histograms needed for
    the fit.

    Arguments:
        - ds: pandas dataframe
            Dataframe containing the event info.
        - name: str
            Name of the process.
        - bins: dict
            Dictionary describing the binning in various observables.
        - bins_2D: list
            List of bins for the 2D histogram.
        - wVar: dict
            Dictionary of weights.
        - observable_q2bins: ?
        - nTotExp: float
            Total number of expected events
        - nTotSelected: float
            Total number of selected events?
    """
    # Add systematic uncertainty terms for PV_x, PV_y, and PV_z. These don't
    # have any special weight, but we instead use q2, Est_mu, and M2_miss with
    # the shifted primary vertex when histogramming the data
    wVar_new = {k: v for k, v in wVar.iteritems()}
    for attr in ['x','y','z']:
        for wVar in ['Up','Down']:
            wVar_new['PV_%s%s' % (attr,wVar)] = np.ones_like(weightsCentral)

    histo = {}
    # Variables in the whole spectrum
    for var in observables_q2integrated:
        if var not in histo:
            histo[var] = {}
        for name_wVar, v_wVar in wVar_new.iteritems():
            if name_wVar:
                h_name = '%s__%s' % (name,name_wVar)
            else:
                h_name = name
            # Compute the overall weights. The weights stored in wVar are of
            # the form weights/central weights to make it easier to combine
            # multiple weights by multiplication.
            w = weightsCentral*v_wVar
            scale = nTotExp/nTotSelected
            if var == 'specQ2':
                varName = 'q2'
            else:
                varName = var

            if 'data' not in name and name_wVar.startswith('PV_') and var in PV_QUANTITIES:
                varName += name_wVar[2:]

            histo[var][h_name] = create_TH1D(ds[varName], name=h_name, weights=w, scale_histo=scale, binning=bins[var], opt='underflow,overflow')

    # Variables to be broken in q2 bins
    for i_q2, (q2_l, q2_h) in enumerate(zip(bins['q2'][:-1],bins['q2'][1:])):
        sel_q2 = (ds['q2'] > q2_l) & (ds['q2'] <= q2_h)
        name2D = 'h2D_q2bin%i' % i_q2
        if not name2D in histo:
            histo[name2D] = {}
        for var in observables_q2bins:
            cat_name = '%s_q2bin%i' % (var,i_q2)

            if cat_name not in histo:
                histo[cat_name] = {}

            for name_wVar, v_wVar in wVar_new.iteritems():
                h_name = name
                if name_wVar != '':
                    h_name += '__%s' % name_wVar
                w = weightsCentral*v_wVar
                scale = nTotExp/nTotSelected

                varName = var
                if 'data' not in name and name_wVar.startswith('PV_') and var in PV_QUANTITIES:
                    varName += name_wVar[2:] 
                    q2 = 'q2%s' % name_wVar[2:]
                else:
                    q2 = 'q2'

                # FIXME: this is inefficient
                sel_q2 = (ds[q2] > q2_l) & (ds[q2] <= q2_h)

                histo[cat_name][h_name] = create_TH1D(ds[varName][sel_q2], name=h_name, title=h_name, binning=bins[var][i_q2], opt='underflow,overflow', weights=w[sel_q2], scale_histo=scale)
                if var == 'M2_miss':
                    if 'data' not in name and name_wVar.startswith('PV_'):
                        auxS = np.column_stack((ds['M2_miss%s' % name_wVar[2:]][sel_q2], ds['Est_mu%s' % name_wVar[2:]][sel_q2]))
                    else:
                        auxS = np.column_stack((ds['M2_miss'][sel_q2], ds['Est_mu'][sel_q2]))
                    histo[name2D][h_name] = create_TH2D(auxS, name=h_name, title=h_name, binning=bins_2D[i_q2], weights=w[sel_q2], scale_histo=scale)

    return histo

def unroll(histo, bins):
    # Do the unrolling
    print '\n\n########### Unrolling 2D histograms ###########'
    unrolledBins = []
    unrollingCutOff = 3
    for i_q2 in range(len(bins['q2'])-1):
        unrolledBins.append([])
        name2D = 'h2D_q2bin%i' % i_q2
        hSum = None
        nDroppedBins = 0
        nExpectedDroppedEvents = 0
        for key, hN in histo[name2D].iteritems():
            if '__' in key:
                continue
            if hSum is None:
                hSum = hN.Clone('hSum_%i' % i_q2)
            else:
                scale = SM_RDst if 'tau' in key else 1.
                hSum.Add(hN, scale)
        for ix in range(1, hSum.GetNbinsX()+1):
            for iy in range(1, hSum.GetNbinsY()+1):
                if hSum.GetBinContent(ix, iy) > unrollingCutOff:
                    unrolledBins[i_q2].append([ix, iy])
                else:
                    nDroppedBins += 1
                    nExpectedDroppedEvents += hSum.GetBinContent(ix, iy)

        print 'Dropped bins:', nDroppedBins
        print 'Expected dropped candidates:', nExpectedDroppedEvents

        nameU = 'Unrolled_q2bin%i' % i_q2
        histo[nameU] = {}
        validBins = unrolledBins[i_q2]
        for n, h in histo[name2D].iteritems():
            hUnrolled = rt.TH1D(h.GetName(), h.GetTitle(), len(validBins), 0.5, len(validBins)+0.5)
            for i, (ix, iy) in enumerate(validBins):
                hUnrolled.SetBinContent(i+1, h.GetBinContent(ix, iy))
                hUnrolled.SetBinError(i+1, h.GetBinError(ix, iy))
            histo[nameU][n] = hUnrolled

    return histo, unrolledBins

def get_ctrl_group(ds):
    """
    Add a column specifying the control region. Here, the control region number
    is sort of like the control region expressed as an integer. It is a three
    digit number where the number in the least significant digit represents the
    charge of the first track (1 for positive, 2 for negative), the next digit
    represents the charge of the second track, etc.

    For example, ctrl == 0 represents the signal region, ctrl == 200 represents
    the minus control region, etc.

    The nice thing about this representation is that if you want to find out what
    would happen if you didn't reconstruct the lowest pt track, you can just
    compute ctrl//10. For example:

        >>> ctrl = 222
        >>> ctrl//10
        22

    This allows us to compute what would happen if events moved between the
    control groups.
    """
    tk0 = np.where(ds['tkCharge_0'] == -1, 2, ds['tkCharge_0']).astype(int)
    tk1 = np.where(ds['tkCharge_1'] == -1, 2, ds['tkCharge_1']).astype(int)
    tk2 = np.where(ds['tkCharge_2'] == -1, 2, ds['tkCharge_2']).astype(int)
    condlist = [ds['N_goodAddTks'] == 0,ds['N_goodAddTks'] == 1,ds['N_goodAddTks'] == 2,ds['N_goodAddTks'] == 3,ds['N_goodAddTks'] > 3]
    choicelist = np.array([np.zeros_like(tk0),tk0,tk0*10+tk1,tk0*100+tk1*10+tk2,tk0*100+tk1*10+tk2])
    return np.select(condlist,choicelist)

def get_min_pt(ds):
    condlist = [ds['N_goodAddTks'] == 0,ds['N_goodAddTks'] == 1,ds['N_goodAddTks'] == 2,ds['N_goodAddTks'] == 3,ds['N_goodAddTks'] > 3]
    choicelist = np.array([np.zeros_like(ds['tkPt_0']),ds['tkPt_0'],ds['tkPt_1'],ds['tkPt_2'],ds['tkPt_2']])
    return np.select(condlist,choicelist)

def get_ctrl_weights(ds,pt_min=0,pt_max=1,fraction=0.3,epsilon=1e-10):
    """
    Returns weights for events which move between control regions due to the
    lowest pt track not passing all cuts. For example, if extra tracks in data
    are less likely to be reconstructed or pass the goodness of fit tests with
    the vertex, they will end up being in a different control region. The
    possible movements of the events are: ppp -> pp, ppm -> pp, pmp -> pm, pmm
    -> pm, mmp -> mm, mmm -> mm, ,pm -> p, mp -> m, p -> signal, m -> signal.

    Returns a tuple (weight, up, down), where weight is the default weights
    (which assigns `epsilon` to all duplicate events and 1 to all original
    events), up is the weights for when events move divided by the original
    weights, and down is the weights for when data is *more* likely to
    reconstruct extra tracks divided by the original weights (currently assumed
    to not happen, since this seems unlikely and there is no way currently to
    know the pt of tracks which *almost* got reconstructed).

    Parameters
    ----------
    ds: dataframe
        Events to calculae weights for.
    pt_min: float
        The minimum pt of events being moved.
    pt_max: float
        The maximum pt of events being moved.
    fraction: float
        The fraction of events whose lowest pt track falls in between `pt_min`
        and `pt_max` which are moved.
    epsilon: float
        Small weight given to duplicate events.
    """
    orig = ds['ctrl'] == ds['ctrl2']
    w = np.where(orig,1,epsilon).astype(float)
    down = w

    # The conditions here are:
    #
    #     1. This is an original event with no extra tracks.
    #     2. This is an original event which got moved.
    #     3. This is an original event which didn't get moved.
    #     4. This is a duplicate event which got moved.
    #     5. This is a duplicate event which didn't get moved.
    pt = (ds['tkPt_last'] > pt_min) & (ds['tkPt_last'] < pt_max)
    condlist = [orig & (ds['ctrl'] == 0),
                orig & pt,
                orig & ~pt,
                ~orig & pt,
                ~orig & ~pt]
    up = np.select(condlist,[1,1-fraction,1,fraction,0])
    return w, up/w, down/w

def createCardName(args):
    c = args.card_tag + args.category + '_' + args.ff_scheme
    if args.decorrelate_ff_pars:
        c += 'decorr'
    if args.freeze_ff:
        c += 'frozen'
    if args.use_mva:
        c += '_MVA'
    if args.asimov:
        c += '_Asimov'
        if args.lumi_mult != 1.:
            c += '{:.0f}'.format(args.lumi_mult)
    if not args.unblinded:
        c += '_blinded'
    if args.no_mc_stats:
        c += '_NoMCstats'
    if not args.free_mu_br:
        c += '_muBrPDG'
    return c

def dumpCallAndGitStatus(args):
    """
    Log command line call and arguments.
    """
    with open(join(webFolder,'callsLog.txt'), 'a') as f:
        f.write(50*'#'+'\n')
        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n')
        f.write( ' '.join(sys.argv) + '\n')
        f.write(5*'>'+' Arguments\n')
        f.write(yaml.dump(args.__dict__))
        f.write(5*'<'+'\n')
        f.write(50*'-'+ '\n')

    # Write git sha1 and any uncommited changes to the web directory
    diff = subprocess.check_output(['git','diff'])
    with open(join(webFolder,'git_diff.log'), 'a') as f:
        f.write('\n' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n')
        f.write(diff + '\n')

    sha1 = subprocess.check_output(['git','show-ref','--head','--hash=8'])
    with open(join(webFolder,'git_sha1.log'), 'a') as f:
        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' --> ' + sha1.decode("utf-8").split('\n')[0] + '\n')

def runCommandSafe(command, printCommand=True):
    """
    Run a combine command and look for error messages. If there are any error
    messages, print out the message and raise an exception.
    """
    if printCommand:
        print command
    status, output = commands.getstatusoutput(command)

    def raiseFlag(inputText):
        flag = 'Warning: Did not find a parameter' in inputText
        flag = flag or ('WARNING: cannot freeze nuisance' in inputText)
        flag = flag or ('WARNING: MultiDimFit failed' in inputText)
        flag = flag or ('ERROR' in inputText and not 'Messages of type ERROR : 0' in inputText)
        flag = flag or ('There was a crash.' in inputText)
        return flag

    flagged = raiseFlag(output)

    if status or flagged:
        print output, '\n\n'
        print '==================================================================='
        print '====================== Breaking the execution ====================='
        print '==================================================================='
        if status:
            print '\033[1m\x1b[31mStatus:\x1b[0m', status

        if flagged:
            for line in output.split('\n'):
                if raiseFlag(line):
                    print '\033[1m\x1b[31mFlagged line:\x1b[0m', line
        raise Exception("command failed!")
    return output

def cleanPreviousResults():
    os.system('rm -v '+card_location.replace('.txt', '*'))

    os.system('rm '+histo_file_dir+os.path.basename(card_location).replace('.txt', '_*'))

    os.system('rm -rf '+outdir)
    os.system('mkdir -p ' + outdir + '/fig')

    os.system('rm -rf '+webFolder)
    os.makedirs(webFolder)
    os.system('cp '+webFolder+'/../index.php '+webFolder)

corrScaleFactors = {}
def loadDatasets(category, loadRD, args):
    print 'Loading MC datasets'
    candDir = 'ntuples_B2DstMu_%s' % NTUPLE_TAG
    print 'Using candDir = %s' % candDir
    print 'Using skim MC = skimmed%s' % args.skim_tag
    print 'Using skim RD = skimmed%s' % args.skim_tag_rd
    MCsample = {
    ######## Signals
    'tau':           DSetLoader('Bd_TauNuDst', candDir=candDir, skim_tag=args.skim_tag),
    'mu':            DSetLoader('Bd_MuNuDst', candDir=candDir, skim_tag=args.skim_tag),
    ######## D** background
    'Bu_MuDstPi':    DSetLoader('Bu_MuNuDstPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_MuDstPi':    DSetLoader('Bd_MuNuDstPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_MuDstPiPi':  DSetLoader('Bd_MuNuDstPiPi_v3', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_MuDstPiPi':  DSetLoader('Bu_MuNuDstPiPi_v3', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_TauDstPi':   DSetLoader('Bu_TauNuDstPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_TauDstPi':   DSetLoader('Bd_TauNuDstPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_TauDstPiPi': DSetLoader('Bd_TauNuDstPiPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_TauDstPiPi': DSetLoader('Bu_TauNuDstPiPi', candDir=candDir, skim_tag=args.skim_tag),
    'Bs_MuDstK':     DSetLoader('Bs_MuNuDstK', candDir=candDir, skim_tag=args.skim_tag),
    'Bs_TauDstK':    DSetLoader('Bs_TauNuDstK', candDir=candDir, skim_tag=args.skim_tag),
    ######## D*Hc background
    'Bd_DstDu': DSetLoader('Bd_DstDu', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_DstDu': DSetLoader('Bu_DstDu', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_DstDd': DSetLoader('Bd_DstDd', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_DstDd': DSetLoader('Bu_DstDd', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_DstDs': DSetLoader('Bd_DstDs', candDir=candDir, skim_tag=args.skim_tag),
    'Bs_DstDs': DSetLoader('Bs_DstDs', candDir=candDir, skim_tag=args.skim_tag),
    'Bd_DDs1':  DSetLoader('Bd_DDs1', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_DDs1':  DSetLoader('Bu_DDs1', candDir=candDir, skim_tag=args.skim_tag),
    #'B_DstDXX': DSetLoader('B_DstDXX', candDir=candDir, skim_tag=args.skim_tag),
    'Bu_D2stDs': DSetLoader('Bu_D2stDs', candDir=candDir, skim_tag=args.skim_tag),
    }

    dSet = {}
    dSetTkSide = {}
    mcType = 'bare' if args.bare_mc else 'corr'
    print 'mcType: %s' % mcType
    if args.max_events is not None:
        print_warning('Warning: limiting events per MC sample to %.0f' % args.max_events)

    relevantBranches = load_yaml('branches_to_load.yaml')

    for n, s in MCsample.iteritems():
        if not n in processOrder:
            raise Exception("%s not declarted in processOrder" % n)

        branches_to_load = relevantBranches['all'] + relevantBranches['mc']
        if n in ['tau', 'mu']:
            branches_to_load += relevantBranches['signal']
        if 'MuDstPi' in n:
            branches_to_load += relevantBranches['DstPi']
        if re.match('B[usd]_DstD[usd]', n):
            branches_to_load += relevantBranches['DstHc']

        filename = s.skimmed_dir + '/{}_{}.root'.format(category.name, mcType)
        dSet[n] = load_data(filename, stop=args.max_events,branches=branches_to_load)
        filename = s.skimmed_dir + '/{}_trkCtrl_{}.root'.format(category.name, mcType)
        dSetTkSide[n] = load_data(filename, stop=args.max_events,branches=branches_to_load)


    dataDir = '/storage/af/group/rdst_analysis/BPhysics/data/cmsRD'
    locRD = dataDir+'/skimmed'+args.skim_tag_rd+'/B2DstMu_SS_{}_{}'.format(NTUPLE_TAG,category.name)
    dSet['dataSS_DstMu'] = load_data(locRD + '_corr.root')
    dSetTkSide['dataSS_DstMu'] = load_data(locRD + '_trkCtrl_corr.root')

    if loadRD:
        print 'Loading real data datasets'

        locRD = dataDir+'/skimmed'+args.skim_tag_rd+'/B2DstMu_{}_{}'.format(NTUPLE_TAG, category.name)
        dSet['data'] = load_data(locRD + '_corr.root', branches=relevantBranches['all'])
        dSetTkSide['data'] = load_data(locRD + '_trkCtrl_corr.root', branches=relevantBranches['all'])

    for name in dSet:
        dSet[name]['ctrl'] = get_ctrl_group(dSet[name])
        dSet[name]['ctrl2'] = dSet[name]['ctrl']
        dSet[name]['tkPt_last'] = get_min_pt(dSet[name])
        dSet[name]['PV_pval'] = 1-scipy_chi2.cdf(dSet[name]['PV_chi2'],dSet[name]['PV_ndof'])

    for name in dSetTkSide:
        dSetTkSide[name]['ctrl'] = get_ctrl_group(dSetTkSide[name])
        dSetTkSide[name]['ctrl2'] = dSetTkSide[name]['ctrl']
        dSetTkSide[name]['tkPt_last'] = get_min_pt(dSetTkSide[name])
        dSetTkSide[name]['PV_pval'] = 1-scipy_chi2.cdf(dSetTkSide[name]['PV_chi2'],dSetTkSide[name]['PV_ndof'])
        if 'data' not in name:
            # Here is where we duplicate the MC to allow us to move events
            # between control regions.
            dup = dSetTkSide[name].copy()
            dup = dup[dup['ctrl'] != 0]
            dup['ctrl2'] = dup['ctrl']//10
            # Set the massHadTks and massVisTks column equal to what it would
            # be had we missed the last track.
            condlist = [dup['ctrl2'] == 0, dup['ctrl2'] < 10, dup['ctrl2'] < 100, dup['ctrl2'] >= 100]
            choicelist = [dup['massHadTks1'], dup['massHadTks1'], dup['massHadTks2'], dup['massHadTks2']]
            dup['massHadTks'] = np.select(condlist,choicelist)
            choicelist = [dup['massVisTks1'], dup['massVisTks1'], dup['massVisTks2'], dup['massVisTks2']]
            dup['massVisTks'] = np.select(condlist,choicelist)
            # Make sure we didn't accidentally copy any events which don't
            # move.
            if (dup['ctrl2'] == dup['ctrl']).any():
                raise Exception("ctrl2 == ctrl!")
            # Now put events in the correct dictionary since we have separate
            # dictionaries for the signal and track control regions
            dSetTkSide[name] = pd.concat((dSetTkSide[name],dup[dup['ctrl2'] != 0]),ignore_index=True)
            if name in dSet:
                dSet[name] = pd.concat((dSet[name],dup[dup['ctrl2'] == 0]),ignore_index=True)

    if args.use_mva:
        fname = '/storage/af/group/rdst_analysis/BPhysics/data/kinObsMVA/clfGBC_tauVall_v3{}.p'.format(category.name)
        clfGBC = pickle.load(open(fname, 'rb'))

        print 'Computing MVA results'
        print 'Inputs:', ' '.join(clfGBC.featuresNames)
        for n in processOrder + ['data']:
            if args.asimov and n == 'data':
                continue
            print n,
            for v in clfGBC.featuresNames:
                if np.sum(np.isnan(dSet[n][v])):
                    print 'Set', n,':', np.sum(np.isnan(dSet[n][v])), 'nan in', v
                    dSet[n] = dSet[n][np.logical_not(np.isnan(dSet[n][v]))]
                if np.sum(np.isnan(dSetTkSide[n][v])):
                    print 'SetTkSide', n,':', np.sum(np.isnan(dSetTkSide[n][v])), 'nan in', v
                    dSetTkSide[n] = dSetTkSide[n][np.logical_not(np.isnan(dSetTkSide[n][v]))]


            dSet[n]['MVA'] = clfGBC.predict_proba(dSet[n][clfGBC.featuresNames])[:,1]
            dSetTkSide[n]['MVA'] = clfGBC.predict_proba(dSetTkSide[n][clfGBC.featuresNames])[:,1]
        print '\n'

    if args.b_reco in ['coll', 'prefit']:
        print '[INFO] Using '+args.b_reco+' B reconstruction'
        for name in dSet:
            dSet[name].drop(columns=['q2', 'Est_mu', 'M2_miss'], inplace=True)
            dSet[name].rename(columns={'q2_'+args.b_reco:'q2', 'Est_mu_'+args.b_reco:'Est_mu', 'M2_miss_'+args.b_reco:'M2_miss'}, inplace=True)
        for name in dSetTkSide:
            dSetTkSide[name].drop(columns=['q2', 'Est_mu', 'M2_miss'], inplace=True)
            dSetTkSide[name].rename(columns={'q2_'+args.b_reco:'q2', 'Est_mu_'+args.b_reco:'Est_mu', 'M2_miss_'+args.b_reco:'M2_miss'}, inplace=True)

    if args.dump_weights_tree:
        print 'Skipping on the flight cuts (if any).'
    else:
        if args.cut_mu_ps:
            addCuts = [ ['M2_miss', 0.4, 1e3] ]
        else:
            addCuts = [ ['M2_miss', -2., 1e3] ]

        addCuts += [
        ['mu_eta', -0.8, 0.8],
        # ['PV_pval', 0, 0.95],
        # ['mu_pt', 0, 20],
        # ['B_eta', -1., 1.],
        # ['pis_pt', 1., 1e3],
        # ['mu_db_iso04', 0, 80],
        ['mu_lostInnerHits', -2, 1],
        # ['K_lostInnerHits', -2, 1],
        # ['pi_lostInnerHits', -2, 1],
        # ['pis_lostInnerHits', -2, 1],
        ['mass_piK', 1.86483-0.035, 1.86483+0.035],
        ['deltaM_DstD', 0.14543-1.e-3, 0.14543+1.e-3],
        ['pval_D0pismu', 0.1, 1.0],
        # ['ctrl_tk_pval_0', 0.2, 1.0],
        # ['ctrl_tk_pval_1', 0.2, 1.0],
        # ['ctrl_pm_massVisTks', 0, 3.8],
        # ['ctrl_pm_massHadTks', 2.6, 10],
        # ['ctrl_pm_index', 3, 0],
        ['sigdxy_vtxD0_BS',10,1e3],
        ]

        if args.use_mva and not args.unblinded:
            print 'Discarding events with good MVA'
            addCuts.append(['MVA', 0, 0.7])

        if len(addCuts) > 0:
            with open(webFolder + '/callsLog.txt', 'a') as f:
                f.write(5*'>'+' Cuts at loading time\n')
                print 5*'>'+' Cuts at loading time'
                for v, m, M in addCuts:
                    if 'index' in v:
                        f.write('{} % {} <= {}\n'.format(v, m, M))
                        print '{} % {} <= {}'.format(v, m, M)
                    else:
                        f.write('{} < {} < {}\n'.format(m, v, M))
                        print '{} < {} < {}'.format(m, v, M)
                f.write(5*'<'+'\n')
                print 5*'<'
                f.write(50*'-'+ '\n')
            for k in dSet.keys():
                sel = np.ones_like(dSet[k]['q2']).astype(np.bool)
                for var, low, high in addCuts:
                    if var.startswith('ctrl_'): continue
                    if var not in dSet[k].columns:
                        raise Exception("%s not in %s main dataset" % (var,k))
                    sel = np.logical_and(sel, np.logical_and(dSet[k][var] > low, dSet[k][var] < high))


                orig = dSet[k]['ctrl'] == dSet[k]['ctrl2']
                dSet[k] = dSet[k][sel]
                # We don't want to include the duplicate events here, so we
                # compute the correction scale factors only for those events
                # which aren't duplicates.
                corrScaleFactors[k] = np.sum(sel[orig])/float(sel[orig].shape[0])

                # Just as a test
                # if 'data' not in k:
                #     sel_mixed = np.logical_and(np.abs(dSet[k]['MC_B_pdgId']) == 511,
                #                                dSet[k]['MC_B_mother_pdgId'] + dSet[k]['MC_B_pdgId'] == 0)
                #     fMixed = np.sum(sel_mixed)/float(sel_mixed.shape[0])
                #     print 'Removing mixed B0 from', k, ': {:.3f}'.format(fMixed)
                #     dSet[k] = dSet[k][np.logical_not(sel_mixed)]

                sel = np.ones_like(dSetTkSide[k]['q2']).astype(np.bool)
                for var, low, high in addCuts:
                    region = 'all'
                    if var.startswith('ctrl_'):
                        var = var.replace('ctrl_', '')
                        if re.match('[pm][pm_]_', var[:3]):
                            region = var[:2]
                            var = var[3:]
                    if var not in dSetTkSide[k].columns:
                        raise Exception("%s not in %s main dataset" % (var,k))
                    if var == 'index':
                        thisSel = np.mod(dSetTkSide[k][var], low) <= high
                    else:
                        thisSel = np.logical_and(dSetTkSide[k][var] > low, dSetTkSide[k][var] < high)
                    if region == 'all':
                        sel = np.logical_and(sel, thisSel)
                    else:
                        auxSel = np.logical_or( np.logical_not(controlRegSel[region](dSetTkSide[k])), thisSel)
                        sel = np.logical_and(sel, auxSel)

                orig = dSetTkSide[k]['ctrl'] == dSetTkSide[k]['ctrl2']
                dSetTkSide[k] = dSetTkSide[k][sel]
                # We don't want to include the duplicate events here, so we
                # compute the correction scale factors only for those events
                # which aren't duplicates.
                corrScaleFactors[k+'_tk'] = np.sum(sel[orig])/float(sel[orig].shape[0])

    for n in dSet.keys():
        dSet[n].reset_index(inplace=True, drop=True)
        dSet[n].reset_index(inplace=True, drop=False)
    for n in dSetTkSide.keys():
        dSetTkSide[n].reset_index(inplace=True, drop=True)
        dSetTkSide[n].reset_index(inplace=True, drop=False)

    return MCsample, dSet, dSetTkSide

def computeBrVarWeights(ds, selItems={}, relScale=0.2, centralVal=1., keepNorm=False, absVal=True):
    sel = np.ones_like(ds['mu_pt']).astype(np.bool)
    for var, val in selItems.iteritems():
        if absVal:
            sel = np.logical_and(np.abs(ds[var].astype(np.int)) == val, sel)
        else:
            sel = np.logical_and(ds[var].astype(np.int) == val, sel)
    w = np.where(sel, centralVal, 1.)
    up = np.where(sel, centralVal*(1.+relScale), 1.)
    down = np.where(sel, centralVal*max(0, 1.-relScale), 1.)
    if keepNorm:
        up = (float(up.shape[0])/np.sum(up)) * up
        down = (float(down.shape[0])/np.sum(down)) * down
    return w, up/w, down/w

def computeWidthVarWeights(ds, selItems=[], newGamma=None, relScale=0.1, keepNorm=False):
    """
    Gamma modification factor.
    """
    # See EvtGen BW implementation:
    # https://github.com/alberto-sanchez/evtgen-cms/blob/master/src/EvtGenBase/EvtBreitWignerPdf.cpp#L41
    w = np.ones_like(ds['mu_pt'])
    up = np.ones_like(ds['mu_pt'])
    down = np.ones_like(ds['mu_pt'])
    for i, (pdgId, mass, gamma) in enumerate(selItems):
        # print pdgId, mass, gamma
        dx2 = np.clip(np.square(ds['MC_MassCharmedBDaughter'] - mass), 0, 9*(gamma**2)) # Non relativistic
        # dx2 = np.clip(np.square(np.square(ds['MC_MassCharmedBDaughter']) - mass**2), 0, 9*((mass*gamma)**2))

        if not (newGamma is None) and not (newGamma[i] is None):
                gNew = newGamma[i]
                wCentral = ((dx2 + (gamma**2)/4.0)*gNew)/(gamma*(dx2 + (gNew**2)/4.0)) # Non relativistic
                # wCentral = (gNew/gamma) * ( (dx2 + (mass*gamma)**2) / (dx2 + (mass*gNew)**2) )
                gUp = gNew*(1+relScale)
                gDown = gNew*(1-relScale)
        else:
            wCentral = np.ones_like(dx2)
            gUp = gamma*(1+relScale)
            gDown = gamma*(1-relScale)

        # Non relativistic
        wUp = ((dx2 + (gamma**2)/4.0)*gUp)/(gamma*(dx2 + (gUp**2)/4.0))
        wDown = ((dx2 + (gamma**2)/4.0)*gDown)/(gamma*(dx2 + (gDown**2)/4.0))
        # wUp = (gUp/gamma) * ( (dx2 + (mass*gamma)**2) / (dx2 + (mass*gUp)**2) )
        # wDown = (gDown/gamma) * ( (dx2 + (mass*gamma)**2) / (dx2 + (mass*gDown)**2) )

        sel = np.abs(ds['MC_DstMotherPdgId'].astype(np.int)) == np.abs(pdgId)
        w = np.where(sel, wCentral, w)
        up = np.where(sel, wUp, up)
        down = np.where(sel, wDown, down)

    if keepNorm:
        w *= float(w.shape[0])/np.sum(w)
        up *= float(w.shape[0])/np.sum(up)
        down *=  float(w.shape[0])/np.sum(down)
    return w, up/w, down/w

def computeRandomTracksWeights(ds, relScale=0.1, centralVal=1., kind=None):
    # selPdgID0 = np.logical_and( ds['MC_tkFlag_0'] == 1, ds['MC_tkFromMainB_0'] == 0)
    # selPdgID1 = np.logical_and( ds['MC_tkFlag_1'] == 1, ds['MC_tkFromMainB_1'] == 0)
    # Track exists and it is not from the decay of the main B
    selPdgID0 = np.logical_and(ds['tkPt_0'] > 0.1, ds['MC_tkFromMainB_0'] < 0.5)
    selPdgID1 = np.logical_and(ds['tkPt_1'] > 0.1, ds['MC_tkFromMainB_1'] < 0.5)
    if kind == 'PU':
        selPdgID0 = np.logical_and(selPdgID0, ds['MC_tkFlag_0'] < 0.5)
        selPdgID1 = np.logical_and(selPdgID1, ds['MC_tkFlag_1'] < 0.5)
    elif kind == 'PV':
        selPdgID0 = np.logical_and(selPdgID0, ds['MC_tkFlag_0'] == 1)
        selPdgID1 = np.logical_and(selPdgID1, ds['MC_tkFlag_1'] == 1)
    else:
        raise Exception("Kind %s not recognized" % kind)
    exponent = selPdgID0.astype(np.int) + selPdgID1.astype(np.int)
    w = np.power(centralVal, exponent)
    up = np.power(centralVal*(1+relScale), exponent)/w
    down = np.power(centralVal*max(0, 1-relScale), exponent)/w
    return w, up, down

def createHistograms(category, args):
    cmd = 'rm -fv '+histo_file_dir+'histos_{}_ready.log'.format(card_name)
    os.system(cmd)

    MCsample, dSet, dSetTkSide = loadDatasets(category, not args.asimov, args)
    mcType = 'bare' if args.bare_mc else 'corr'
    ######################################################
    ########## Load calibrations
    ######################################################
    from pileup_utilities import pileupReweighter
    skimmedFile_loc = MCsample['tau'].skimmed_dir + '/{}_{}.root'.format(category.name, mcType)
    puReweighter = pileupReweighter(skimmedFile_loc, 'hAllNTrueIntMC', trg=category.trg)

    fname = '/storage/af/group/rdst_analysis/BPhysics/data/calibration/beamSpot/crystalball_calibration_v2_bs_'+category.name.capitalize()+'.yaml'
    beamSpotParam = load_yaml(fname)

    dataDir = '/storage/af/group/rdst_analysis/BPhysics/data'
    with open(dataDir+'/forcedDecayChannelsFactors_v2.pickle', 'rb') as f:
        decayBR = pickle.load(f)

    loc = dataDir+'/calibration/triggerScaleFactors/'
    fTriggerSF = rt.TFile.Open(loc + 'HLT_' + category.trg + '_SF_%s.root' % TRIGGER_SCALE_FACTOR, 'READ')
    hTriggerSF = fTriggerSF.Get('hSF_HLT_' + category.trg)
    def computeTrgSF(ds, hSF, selection=None):
        trgSF = np.ones_like(ds['q2'])
        trgSFUnc = np.zeros_like(ds['q2'])
        ptmax = hSF.GetXaxis().GetXmax() - 0.01
        ipmax = hSF.GetYaxis().GetXmax() - 0.01
        etamax = hSF.GetZaxis().GetXmax() - 0.01
        x = np.column_stack((ds['mu_pt'], ds['mu_eta'], ds['mu_sigdxy']))
        if not selection is None:
            x = x[selection]
        inBoundary = np.all([
            ds['mu_pt'] > hTriggerSF.GetXaxis().GetBinCenter(1),
            ds['mu_pt'] < hTriggerSF.GetXaxis().GetBinCenter(hTriggerSF.GetNbinsX()),
            ds['mu_sigdxy'] > hTriggerSF.GetYaxis().GetBinCenter(1),
            ds['mu_sigdxy'] < hTriggerSF.GetYaxis().GetBinCenter(hTriggerSF.GetNbinsY()),
            np.abs(ds['mu_eta']) > hTriggerSF.GetZaxis().GetBinCenter(1),
            np.abs(ds['mu_eta']) < hTriggerSF.GetZaxis().GetBinCenter(hTriggerSF.GetNbinsZ())
        ],axis=0)
        for i, (pt, eta, ip) in enumerate(x):
            ix = hSF.GetXaxis().FindBin(min(ptmax, pt))
            iy = hSF.GetYaxis().FindBin(min(ipmax, ip))
            iz = hSF.GetZaxis().FindBin(min(etamax, np.abs(eta)))
            # Make sure all the values are in between the bincenters, otherwise
            # the interpolation doesn't work.
            if inBoundary[i]:
                trgSF[i] = hSF.Interpolate(pt,ip,np.abs(eta))
            else:
                trgSF[i] = hSF.GetBinContent(ix, iy, iz)
            ib = hSF.GetBin(ix, iy, iz)
            trgSFUnc[i] = hSF.GetBinError(ib)
            if trgSF[i] == 0:
                print pt, ip, np.abs(eta)
                raise Exception("trigger scale factor equals zero!")

        # Divide them for the weight so later you can simply multiply back to get the value
        up = 1 + trgSFUnc/trgSF
        down = 1 - trgSFUnc/trgSF
        return trgSF, up, down

    fMuonIDSF = rt.TFile.Open(dataDir+'/calibration/muonIDscaleFactors/Run2018ABCD_SF_MuonID_Jpsi.root', 'READ')
    hMuonIDSF = fMuonIDSF.Get('NUM_MediumID_DEN_genTracks_pt_abseta')
    def computeMuonIDSF(ds, selection=None):
        muonSF = np.ones_like(ds['q2'])
        muonSFUnc = np.zeros_like(ds['q2'])
        ptmax = hMuonIDSF.GetXaxis().GetXmax() - 0.01
        etamax = hMuonIDSF.GetYaxis().GetXmax() - 0.01
        x = np.column_stack((ds['MC_mu_pt'], ds['MC_mu_eta']))
        if selection is not None:
            x = x[selection]
        for i, (pt, eta) in enumerate(x):
            ix = hMuonIDSF.GetXaxis().FindBin(min(pt, ptmax))
            if ix == 0:
                # Remove underflows (Meaning that the MC matching failed)
                ix = 1
            iy = hMuonIDSF.GetYaxis().FindBin(min(np.abs(eta), etamax))
            muonSF[i] = hMuonIDSF.GetBinContent(ix, iy)
            muonSFUnc[i] = hMuonIDSF.GetBinError(hMuonIDSF.GetBin(ix, iy))
            if muonSF[i] == 0:
                print pt, eta
                print ix, iy
                raise Exception("muon scale factor equals zero!")
        up = 1 + muonSFUnc/muonSF
        down = 1 - muonSFUnc/muonSF
        return muonSF, up, down

    # Kinematic calibration of Bd
    if args.cal_b_pt:
        cal_pT_Bd = kinCalReader(calibration_file=dataDir+'/calibration/kinematicCalibration_Bd/pt_polyCoeff_%s_%s.pkl' % (category.name, BD_CALIBRATION))
    else:
        print 'Not using any B pT calibration'

    cal_eta_B = kinCalReader(calibration_file=dataDir+'/calibration/kinematicCalibration_Bd/eta_polyCoeff_%s_%s.pkl' % (category.name, BD_CALIBRATION))
    cal_addTK_pt = kinCalReader(calibration_file=dataDir+'/calibration/kinematicCalibration_Bd/addTk_pt_polyCoeff_%s_%s.pkl' % (category.name, BD_CALIBRATION))

    def computeKinCalWeights(ds, var, tag, kinCal):
        if kinCal.kind == 'poly':
            # The denominator (sum of weights) for this weights is not known but it cancel out in the ratio
            w = kinCal.getWeights(ds[var], shape=0)
            if np.sum(w == 0):
                print np.sum(w == 0)
                raise Exception("some weights are zero!")

            varDic = {}
            for iShape in range(1, kinCal.nVar+1):
                varDic[tag+'_lam{}Down'.format(iShape)] = kinCal.getWeights(ds[var], shape= -iShape, scale=1.)/w
                varDic[tag+'_lam{}Up'.format(iShape)] = kinCal.getWeights(ds[var], shape= iShape, scale=1.)/w

            return w, varDic
        elif kinCal.kind == 'ratio':
            w = kinCal.f['C'](ds[var])
            if np.sum(w == 0):
                print np.sum(w == 0)
                raise Exception("some weights are zero!")
            up = kinCal.f['Up'](ds[var])/w
            down = kinCal.f['Down'](ds[var])/w
            return w, up, down
        else:
            raise Exception("Unknown calibration")

    parsSoftTracks = {'s':[0.2, 0.15], 'w':[0.9, 0.05]}
    def fSoftTrackEff(x, w, s):
        # Model beta
        x = [0, 3.5, 1.5]
        yLin = x[2]*(1 - w)/x[1] + w
        y = [w, 1., yLin + s*(1-yLin) ]
        beta = np.polyfit(x, y, 2)

        sel = np.logical_or(x < 0.2, x > 3.5)
        return np.where(sel, np.ones_like(x), np.polyval(beta, x))

    softPtUnc = [[0.5, 0.6, 0.10],
                 [0.6, 0.7, 0.07],
                 [0.7, 0.8, 0.05],
                 [0.8, 0.9, 0.04],
                 [0.9, 1.0, 0.03],
                 [1.0, 1.2, 0.02],
                 [1.2, 2.0, 0.01],
                 ]
    def binnnedSoftTrackEff(x, bin, size):
        sel = np.logical_or(x < softPtUnc[bin][0], x > softPtUnc[bin][1])
        return np.where(sel, np.ones_like(x), 1+size*softPtUnc[bin][2])

    def weightsSoftTrackEff(ds, ptList, w=None, s=None, bin=None, size=None):
        if w is None or s is None:
            weight = np.ones_like(ds['mu_pt'])
            for v in ptList:
                weight *= binnnedSoftTrackEff(ds[v], bin, size)
        else:
            weight = np.ones_like(ds['mu_pt'])
            for v in ptList:
                weight *= fSoftTrackEff(ds[v], w, s)
        return weight

    histo = {}
    eventCountingStr = {}

    ######################################################
    ########## Signal region
    ######################################################
    n_q2bins = len(binning['q2'])-1
    if args.b_reco in ['coll', 'prefit']:
        binning['M2_miss'] = [
                array('d', list(np.arange(0., 1., 0.02)) + [1.2] ),
                array('d', list(np.arange(0.5, 2.2, 0.025)) + [2.2] ),
                array('d', list(np.arange(1.5, 6, 0.15)) + [8] ),
                array('d', list(np.arange(5.5, 8.5, 0.15)) + [9] ),
            ]

        binning['Est_mu'] = [
                array('d', list(np.arange(1.7, 2.3, 0.025)) + [2.4] ),
                array('d', list(np.arange(1.35, 1.9, 0.025))),
                array('d', list(np.arange(0.6, 1.6, 0.025))),
                array('d', list(np.arange(0.3, 0.85, 0.03)) + [0.85]),
            ]

        binning_2D = [
            [
                array('d', list(np.arange(0., 1., 0.04)) + [1.2] ),
                array('d', list(np.arange(1.7, 2.3, 0.05)) + [2.4] )
            ],
            [
                array('d', list(np.arange(0.5, 2.2, 0.05)) + [2.2] ),
                array('d', list(np.arange(1.35, 1.9, 0.05)) )
            ],
            [
                array('d', list(np.arange(1.5, 6, 0.25)) + [8] ),
                array('d', list(np.arange(0.6, 1.6, 0.07))),
            ],
            [
                array('d', list(np.arange(5.5, 8.5, 0.3)) + [9] ),
                array('d', list(np.arange(0.3, 0.85, 0.07))  + [0.85] )
            ]

        ]
    else:
        lowSide = []
        if not args.cut_mu_ps:
            lowSide = [-2.0, -1.0, -0.6, -0.4, -0.2, 0., 0.1, 0.2, 0.3]
        binning['M2_miss'] = [
                array('d', lowSide + [0.4, 0.5, 0.75, 1, 1.5, 4] ),
                array('d', lowSide + list(np.arange(0.4, 3.5, 0.2)) + [8] ),
                array('d', lowSide + list(np.arange(0.4, 6, 0.2)) + [8] ),
                array('d', lowSide + list(np.arange(0.4, 7.8, 0.2)) + [8] ),
            ]

        binning['Est_mu'] = [
                array('d', [0.3] + list(np.arange(0.5, 2.2, 0.05)) + [2.3] ),
                array('d', [0.3] + list(np.arange(0.5, 2.2, 0.05)) + [2.3] ),
                array('d', [0.3] + list(np.arange(0.5, 2.2, 0.05)) + [2.2] ),
                [24, 0.3, 2.0],
            ]


        lowSide = [[], []]
        if not args.cut_mu_ps:
            lowSide = [[-2.0, -1.0, -0.4,  -0.2, 0., 0.1, 0.2, 0.3], [-1.0, -0.4, -0.2, 0.]]

        binning_2D = [
            [
                array('d', lowSide[0] + [0.4, 0.5, 0.75, 1, 1.5, 4] ),
                array('d', [0.3] + list(np.arange(0.7, 2.3, 0.3)) + [2.3] )
            ],
            [
                array('d', lowSide[0] + list(np.arange(0.4, 3.0, 0.2)) + [8] ),
                array('d', [0.3] + list(np.arange(0.7, 2.2, 0.3)) )
            ],
            [
                array('d', lowSide[1] + list(np.arange(0.4, 5.6, 0.4)) + [8] ),
                array('d', [0.3] + list(np.arange(0.5, 2.1, 0.3)) + [2.1] )
            ],
            [
                array('d', lowSide[1] + list(np.arange(0.4, 7.6, 0.4)) + [8] ),
                array('d', list(np.linspace(0.3, 2.0, 10)) )
            ]

        ]

    binning['MVA'] = [10, 0, 1]
    if args.use_mva:
        bbb = np.arange(0., np.max(dSet['tau']['MVA']) + 0.0249, 0.025)
        binning['MVA'] = array('d', list(bbb))
    binning['specQ2'] = array('d', list(np.arange(-2, 11.4, 0.2)))
    # binning['mu_sigIP3D_vtxDst'] = 4*[[70, -4, 4]]
    # binning['U_miss'] = 4*[[30, -0.1, 0.18]]
    binning['B_pt'] = array('d', list({'Low': np.arange(10, 75, 2), 'Mid': np.arange(14, 90, 2), 'High': np.arange(18, 110, 2)}[category.name]))
    binning['dxy_vtxD0pismu_PV'] = array('d', list({'Low': np.arange(0, 10, 1), 'Mid': np.arange(0, 10, 1), 'High': np.arange(0, 10, 1)}[category.name]))
    binning['sigdxy_vtxD0_BS'] = array('d', list({'Low': np.arange(0, 100, 1), 'Mid': np.arange(0, 100, 1), 'High': np.arange(0, 100, 1)}[category.name]))
    binning['PV_ndof'] = array('d', list({'Low': np.linspace(0, 200, 100), 'Mid': np.linspace(0, 200, 100), 'High': np.linspace(0, 200, 100)}[category.name]))
    binning['PV_chi2'] = array('d', list({'Low': np.linspace(0, 200, 100), 'Mid': np.linspace(0, 200, 100), 'High': np.linspace(0, 200, 100)}[category.name]))
    binning['PV_pval'] = array('d', list({'Low': np.arange(0, 1, 0.01), 'Mid': np.arange(0, 1, 0.01), 'High': np.arange(0, 1, 0.01)}[category.name]))
    binning['pval_piK'] = array('d', list({'Low': np.arange(0, 1, 0.01), 'Mid': np.arange(0, 1, 0.01), 'High': np.arange(0, 1, 0.01)}[category.name]))
    binning['pval_D0pis'] = array('d', list({'Low': np.arange(0, 1, 0.01), 'Mid': np.arange(0, 1, 0.01), 'High': np.arange(0, 1, 0.01)}[category.name]))
    binning['pval_D0pismu'] = array('d', list({'Low': np.arange(0, 1, 0.01), 'Mid': np.arange(0, 1, 0.01), 'High': np.arange(0, 1, 0.01)}[category.name]))
    binning['mu_pt'] = array('d',
                        {'Low': list(np.arange(7.2, 9.201, 0.05)),
                        'Mid': list(np.arange(9.2, 12.201, 0.05)),
                        'High': list(8+np.logspace(np.log10(12.2-8), np.log10(60), 30)) if np.max(dSet['mu']['mu_pt']) > 20 else list(np.arange(12.2, 20.01, 0.15))
                        }[category.name])
    # binning['Dst_pt'] = array('d', list(np.arange(5, 50, 1)) )
    binning['K_pt'] = array('d', list(np.arange( *({'Low':[0.8, 15, 0.4], 'Mid':[0.8, 20, 0.4], 'High':[0.8, 30, 0.4]}[category.name]) ) ) )
    binning['pi_pt'] = array('d', list(np.arange( *({'Low':[0.8, 15, 0.4], 'Mid':[0.8, 20, 0.4], 'High':[0.8, 30, 0.4]}[category.name]) ) ) )
    binning['pis_pt'] = array('d', list(np.arange(0.5, 5., 0.1)) )

    binning['B_eta'] = array('d', list(np.arange(-1.9, 1.91, 0.05)) )
    binning['mass_piK'] = [75, 1.81, 1.92]
    binning['deltaM_DstD'] = [75, 0.1434, 0.1475]
    binning['mass_D0pismu'] = [50, 2.1, 5.28]
    # binning['dxy_vtxD0pismu_PV'] = [80, 0., 2.5]

    observables_q2bins = []
    observables_q2integrated = []
    if n_q2bins == 3:
        raise Exception("Number of q2 bins is 3. Fix this because it will break!")
    for v, bbb in binning.iteritems():
        if v in ['q2']:
            continue
        elif v == 'MVA' and not args.use_mva:
            continue
        elif len(bbb) == n_q2bins and (isinstance(bbb[0], list) or isinstance(bbb[0], array)):
            observables_q2bins.append(v)
        elif (len(bbb) == 3 and isinstance(bbb, list)) or isinstance(bbb, array):
            observables_q2integrated.append(v)
        else:
            print v, bbb
            raise Exception("Binning not recognized")
    print 'Signal region observables divided in q2 bins', len(observables_q2bins), ':', ' '.join(observables_q2bins)
    print 'Signal region observables integrated in q2', len(observables_q2integrated), ':', ' '.join(observables_q2integrated)


    totalCounting = [0,0]
    print '---------> Fill signal region histograms'
    for n in processOrder:
        ds = dSet[n]
        if n == 'data':
            continue
        print '\n----------->', n, '<-------------'
        wVar = {}
        weights = {}
        if 'data' not in n:
            weights['ctrl'], wVar['ctrlUp'], wVar['ctrlDown'] = get_ctrl_weights(ds)
        if n == 'dataSS_DstMu':
            nTotSelected = ds['q2'].shape[0]
            nTotExp = ds['q2'].shape[0]
        else:
            sMC = MCsample[n]

            orig = ds['ctrl'] == ds['ctrl2']
            nTotSelected = ds[orig]['q2'].shape[0]
            print 'N tot selected: {:.1f}k'.format(1e-3*nTotSelected)
            totalCounting[1] += 1e-3*nTotSelected
            nGenExp = sMC.effMCgen['xsec'][0] * expectedLumi[category.name] * data_over_MC_overallNorm
            eff = [1, 0]
            for f, df in [sMC.effMCgen['effGEN'], decayBR[n], sMC.effCand['effCAND'], sMC.getSkimEff(category.name+'_'+mcType)]:
                eff[0] *= f
                eff[1] += np.square(df/f)
            eff[1] = eff[0] * np.sqrt(eff[1])
            if n in corrScaleFactors.keys():
                eff[0] *= corrScaleFactors[n]
                print 'Using scale factor from a posteriori selection: {:.3f}'.format(corrScaleFactors[n])
            nTotExp = nGenExp*eff[0]
        print 'N tot expected (before weights): {:.2f}k'.format(1e-3*nTotExp)

        if not 'data' in n:
            print 'Including pileup reweighting'
            weights['pileup'] = puReweighter.getPileupWeights(ds['MC_nInteractions'])

            if args.beamspot_calibration:
                print 'Including beam spot correction'
                weights['beamSpot'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs')
                wVar[category.name+'BSyUp'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=0, dmu_y=8)/weights['beamSpot']
                wVar[category.name+'BSyDown'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=0, dmu_y=-8)/weights['beamSpot']
                wVar[category.name+'BSxUp'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=4, dmu_y=0)/weights['beamSpot']
                wVar[category.name+'BSxDown'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=-4, dmu_y=0)/weights['beamSpot']

            print 'Including trigger corrections'
            nameSF = 'trg{}SF'.format(category.trg)
            weights[nameSF], wSfUp, wSfDw = computeTrgSF(ds, hTriggerSF)
            auxOnes = np.ones_like(wSfUp)
            # for i_eta in range(1, hTriggerSF.GetNbinsZ()+1):
            #     c_eta = hTriggerSF.GetZaxis().GetBinCenter(i_eta)
            #     w_eta = hTriggerSF.GetZaxis().GetBinWidth(i_eta)
            # for i_ip in range(1, hTriggerSF.GetNbinsY()+1):
            #     c_ip = hTriggerSF.GetYaxis().GetBinCenter(i_ip)
            #     w_ip = hTriggerSF.GetYaxis().GetBinWidth(i_ip)
            #     if c_ip + 0.5*w_ip <= category.minIP:
            #         continue
            for i_pt in range(1, hTriggerSF.GetNbinsX()+2):
                if i_pt > hTriggerSF.GetNbinsX() and category.name == 'High':
                    sel = ds['mu_pt'] > hTriggerSF.GetXaxis().GetXmax()
                else:
                    c_pt = hTriggerSF.GetXaxis().GetBinCenter(i_pt)
                    w_pt = hTriggerSF.GetXaxis().GetBinWidth(i_pt)
                    if (c_pt + 0.5*w_pt <= category.min_pt) or (c_pt - 0.5*w_pt >= category.max_pt):
                        continue

                    sel = np.abs(ds['mu_pt'] - c_pt) < w_pt
                # sel = np.logical_and(sel, np.abs(ds['mu_sigdxy'] - c_ip) < w_ip)
                # sel = np.logical_and(sel, np.abs(ds['mu_eta'] - c_eta) < w_eta)
                # binName = '_pt{}ip{}eta{}'.format(i_pt, i_ip, i_eta)
                # print 'Trg SF', i_pt, i_ip, i_eta, '-> selected {}'.format(np.sum(sel))
                # binName = '_pt{}ip{}'.format(i_pt, i_ip)
                binName = '_pt{}'.format(i_pt)
                wVar[nameSF+binName+'Up'] = np.where(sel, wSfUp, auxOnes)
                wVar[nameSF+binName+'Down'] = np.where(sel, wSfDw, auxOnes)

            print 'Including muon ID corrections'
            weights['muonIdSF'], _, _ = computeMuonIDSF(ds)

            print 'Including soft track pT corrections'
            partList = ['K_pt', 'pi_pt', 'pis_pt']
            for nBin in range(len(softPtUnc)):
                refPt = '{:.0f}'.format(np.round(np.mean(softPtUnc[nBin][:-1])*1e3))
                wVar['softTrkEff_'+refPt+'Up'] = weightsSoftTrackEff(ds, partList, bin=nBin, size=+1)
                wVar['softTrkEff_'+refPt+'Down'] = weightsSoftTrackEff(ds, partList, bin=nBin, size=-1)


            ############################
            # B phase space corrections
            ############################
            print 'Including B eta corrections'
            cname = 'B_eta'+category.name
            weights[cname], auxVarDic = computeKinCalWeights(ds, 'MC_B_eta', cname, cal_eta_B)
            wVar.update(auxVarDic)

            if args.cal_b_pt and n in samples_Bd:
                print 'Including Bd pT corrections'
                cname = 'BdpT'+category.name
                if cal_pT_Bd.kind == 'ratio':
                    weights[cname], wVar[cname+'Up'], wVar[cname+'Down'] = computeKinCalWeights(ds, 'MC_B_pt', None, cal_pT_Bd)
                else:
                    weights[cname], auxVarDic = computeKinCalWeights(ds, 'MC_B_pt', cname, cal_pT_Bd)
                    wVar.update(auxVarDic)

        ############################
        # Form factor correction
        ############################
        if n in ['mu', 'tau'] and args.ff_scheme != 'NoFF':
            print 'Including B-> D*EllNu FF corrections (Hammer)'
            weights['B2DstFF'] = (ds['wh_'+args.ff_scheme+'Central']*sMC.effCand['rate_den']/sMC.effCand['rate_'+args.ff_scheme+'Central']).values
            for nPar in FreeParFF[args.ff_scheme]:
                for var in ['Up', 'Down']:
                    tag = args.ff_scheme + nPar + var
                    wVar['B2Dst'+tag] = ds['wh_'+tag]/ds['wh_'+args.ff_scheme+'Central']
                    wVar['B2Dst'+tag] *= sMC.effCand['rate_'+args.ff_scheme+'Central']/sMC.effCand['rate_' + tag]
                    wVar['B2Dst'+tag][np.isnan(wVar['B2Dst'+tag])] = 0.

        if '_MuDstPi' in n:
            print 'Including B -> D**MuNu FF corrections (Hammer)'
            weights['purgeUwantedEvts'] = np.array(ds['procId_Dstst'] >= 0).astype(np.int)
            procId_Dstst = np.array(ds['procId_Dstst']).astype(np.int)

            # FIXME: Do we need to calculate the rates ratio for the other
            # processes?
            ratesRatio = np.ones(50)
            ratesRatio[1] = sMC.effCand['rate_den_D1']/sMC.effCand['rate_D1BLR_central']
            ratesRatio[2] = sMC.effCand['rate_den_D1st']/sMC.effCand['rate_D1stBLR_central']
            ratesRatio[3] = sMC.effCand['rate_den_D2st']/sMC.effCand['rate_D2stBLR_central']

            number_invalid = np.count_nonzero(ds['procId_Dstst'] < 0)
            if number_invalid > 0:
                print_warning("Warning: There are %i events in the B -> Mu D* pi sample with a process id of less than zero!" % number_invalid)

                # ratesRatio doesn't have an index for unknown events with
                # procId_Dstst of -100. These events get a weight of 0 anyways,
                # so we just set the FF weight to 1 to prevent an error.
                # FIXME: Is there a better way to do this?
                procId_Dstst[procId_Dstst < 0] = len(ratesRatio) - 1

            selDstst = np.logical_and(procId_Dstst >= 1, procId_Dstst <= 3)
            weights['B2DststFF'] = np.where(selDstst, ds['wh_Dstst_BLRCentral']*ratesRatio[procId_Dstst], 1.)

            selDstst = np.logical_or(procId_Dstst == 1, procId_Dstst == 3)
            for nEig in [1,2,3,4]:
                for var in ['Up', 'Down']:
                    tag = 'DststN_BLReig'+str(nEig)+var
                    ratesRatio = np.ones(50)
                    ratesRatio[1] = sMC.effCand['rate_D1BLR_central']/sMC.effCand['rate_D1BLR_eig'+str(nEig)+var]
                    ratesRatio[3] = sMC.effCand['rate_D2stBLR_central']/sMC.effCand['rate_D2stBLR_eig'+str(nEig)+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_Dstst_BLRCentral'])*ratesRatio[procId_Dstst], 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.

            selDstst = procId_Dstst == 2
            for nEig in [1,2,3]:
                for var in ['Up', 'Down']:
                    tag = 'DststW_BLReig'+str(nEig)+var
                    ratesRatio = sMC.effCand['rate_D1stBLR_central']/sMC.effCand['rate_D1stBLR_eig'+str(nEig)+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_Dstst_BLRCentral'])*ratesRatio, 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.

        if n.endswith('_MuDstPiPi') and not args.skip_blop:
            print 'Including B -> D(2S)MuNu FF corrections (Hammer)'
            procId_Dstst = np.array(ds['procId_Dstst']).astype(np.int)

            ratesRatio = np.ones(50)
            ratesRatio[4] = sMC.effCand['rate_den_D2sst']/sMC.effCand['rate_D2sstBLOP_central']
            ratesRatio[11] = ratesRatio[4]
            ratesRatio[12] = ratesRatio[4]
            ratesRatio[13] = ratesRatio[4]
            ratesRatio[23] = sMC.effCand['rate_den_D2s']/sMC.effCand['rate_D2sBLOP_central']

            selDstst = np.logical_and(procId_Dstst >= 4, procId_Dstst <= 23)
            weights['B2D2S_FF'] = np.where(selDstst, ds['wh_D2S_BLOPCentral']*ratesRatio[procId_Dstst], 1.)

            for parName in ['RhoSq', 'chi11', 'chi21', 'chi31', 'eta1']:
                for var in ['Up', 'Down']:
                    tag = 'D2S_BLOP'+parName+var
                    ratesRatio = np.ones(50)
                    ratesRatio[4] = sMC.effCand['rate_D2sstBLOP_central']/sMC.effCand['rate_D2sstBLOP_'+parName+var]
                    ratesRatio[11] = ratesRatio[4]
                    ratesRatio[12] = ratesRatio[4]
                    ratesRatio[13] = ratesRatio[4]
                    ratesRatio[23] = sMC.effCand['rate_D2sBLOP_central']/sMC.effCand['rate_D2sBLOP_'+parName+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_D2S_BLOPCentral'])*ratesRatio[procId_Dstst], 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.


        ############################
        # Cleaning signal form slipped in events
        ############################
        if n in ['mu', 'tau']:
            weights['kill_slipped_in'] = np.where( ds['procId_signal'] == {'tau': 0, 'mu': 1}[n], 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

        ############################
        # Dstst resonance mix
        ############################
        if re.match('B[du]_MuDstPi\Z', n):
            print 'Including D**->D*Pi branching ratio and width variations'

            # FIXME: should this include 0?
            weights['kill_slipped_in'] = np.where( np.logical_and(ds['procId_Dstst'] >= 0, ds['procId_Dstst'] <= 3), 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstPi_mix:
                wc, wu, wd = computeBrVarWeights(ds, {'procId_Dstst': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'brB_DstPiMuNu_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd


            nn = 'D2420_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[10423, 2.421, 0.0274], [10413, 2.423, 0.020]],
                                                                                 newGamma=[0.030, 0.025], relScale=0.15)

            nn = 'D2430_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[20423, 2.445, 0.250], [20413, 2.445, 0.250]],
                                                                                 newGamma=[0.3, 0.3], relScale=0.1)

            nn = 'D2460_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[425, 2.461, 0.049], [415, 2.460, 0.037]],
                                                                                 newGamma=[0.045, 0.045], relScale=0.15)

        if not re.search('MuDstPiPi\Z', n) is None:
            print 'Including B -> MuNuD*PiPi width and Br variations'
            widthMods = [[x, 2.640, 0.300] for x in [100413, 100423, 100411, 100421]]
            uncN = 'Dst2S_width'
            weights[uncN], wVar[uncN+'Up'], wVar[uncN+'Down'] = computeWidthVarWeights(ds, selItems=widthMods, relScale=0.3)

            weights['kill_slipped_in'] = np.where( ds['procId_Dstst'] >= 0, 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstPiPi_mix:
                wc, wu, wd = computeBrVarWeights(ds, {'procId_Dstst': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'brB_DstPiPiMuNu_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd


        ############################
        # Hc mix variations
        ############################
        if n in DstHc_sample_id.keys():
            print 'Including', n, 'mix variations'
            weights['kill_slipped_in'] = np.where( np.floor_divide(ds['procId_DstHc'], 100) == DstHc_sample_id[n], 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            invalid_ids = ds['procId_DstHc'][np.floor_divide(ds['procId_DstHc'], 100) != DstHc_sample_id[n]]

            values, counts = np.unique(invalid_ids, return_counts=True)

            print_warning("invalid ids with counts: %s" % sorted(zip(values,counts),key=lambda x: x[1]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstHc_mix:
                if np.floor_divide(proc_id, 100) != DstHc_sample_id[n]: continue
                wc, wu, wd = computeBrVarWeights(ds, {'procId_DstHc': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'br'+n+'_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd

        if n == 'B_DstDXX':
            nnn = 'Bu_DstDXX_frac'
            weights[nnn], wVar[nnn+'Up'], wVar[nnn+'Down'] = computeBrVarWeights(ds, {'MC_DstMotherPdgId': 521}, relScale=0.5, centralVal=2., keepNorm=True)


        ############################
        # Creating histograms
        ############################

        print 'Computing total weights'
        weightsCentral = np.ones_like(ds['q2'])
        for wName, w in weights.iteritems():
            if np.max(np.abs(w)) > 3:
                # FIXME: some weights are dataframes?
                iMax = np.argmax(np.abs(w))
                print '[WARNING] Max weights', wName, ': {:.3}'.format(w[iMax])
            weightsCentral *= w
        print 'N tot expected (after weights): {:.3f}k'.format(1e-3*nTotExp*np.sum(weightsCentral)/nTotSelected)
        totalCounting[0] += 1e-3*nTotExp*np.sum(weightsCentral)/nTotSelected
        evCountStr = '{:.2f} ({:.2f})'.format(1e-3*nTotExp*np.sum(weightsCentral)/nTotSelected, 1e-3*nTotSelected)
        eventCountingStr[n] = evCountStr
        wVar[''] = np.ones_like(weightsCentral)

        if args.dump_weights_tree and not 'data' in n:
            weightsDir = os.path.join(MCsample[n].skimmed_dir, 'weights')
            if not os.path.isdir(weightsDir):
                os.makedirs(weightsDir)

            mcType = 'bare' if args.bare_mc else 'corr'
            auxName = category.name + '_' + mcType + '_' + card_name + '.root'

            wDf = pd.DataFrame.from_dict({'central': weightsCentral*nTotExp/nTotSelected})
            for k, w in wVar.iteritems():
                if k:
                    wDf[k] = w*wDf['central']
            print 'Dumping weights tree in', os.path.join(weightsDir, auxName)
            rtnp.array2root(wDf.to_records(), os.path.join(weightsDir, auxName), treename='weights', mode='RECREATE')

        new_histo = histogram(ds, n, binning, binning_2D, weightsCentral, wVar, observables_q2bins, observables_q2integrated, nTotExp, nTotSelected)

        histo = update(histo,new_histo)

    histo, unrolledBins = unroll(histo, binning)

    if not args.dump_weights_tree:
        with open(outdir+'/unrolledBinsMap.pkl', 'wb') as f:
            pickle.dump(unrolledBins, f)

    evCountStr = '{:.1f} ({:.1f})'.format(*totalCounting)
    eventCountingStr['tot'] = evCountStr

    ######################################################
    ########## Control region
    ######################################################
    ctrlVar = {}
    ctrlVar['ctrl_p__mHad'] = 'massHadTks'
    binning['ctrl_p__mHad'] = [35, 2.13, 2.83]

    ctrlVar['ctrl_p__mVis'] = 'massVisTks'
    binning['ctrl_p__mVis'] = array('d', [2.8] + list(np.arange(3., 5.5, 0.12)) + [5.5] )

    ctrlVar['ctrl_m__mHad'] = 'massHadTks'
    binning['ctrl_m__mHad'] = [40, 2.1, 3.3]

    ctrlVar['ctrl_pm_mVis'] = 'massVisTks'
    binning['ctrl_pm_mVis'] = array('d', [2.8] + list(np.arange(3., 5.5, 0.12)) + [5.5] )

    ctrlVar['ctrl_pm_mHad'] = 'massHadTks'
    binning['ctrl_pm_mHad'] = [50, 2.3, 3.8]

    ctrlVar['ctrl_mm_mHad'] = 'massHadTks'
    binning['ctrl_mm_mHad'] = [20, 2.25, 3.6]

    ctrlVar['ctrl_pp_mHad'] = 'massHadTks'
    binning['ctrl_pp_mHad'] = [20, 2.25, 3.7]

    # ctrlVar['ctrl_m__tk_pt_0'] = 'tkPt_0'
    # binning['ctrl_m__tk_pt_0'] = array('d', list(np.arange(.5, 2., 0.1)) + list(np.arange(2., 7.51, 0.25)) )
    # ctrlVar['ctrl_mm_tk_pt_0'] = 'tkPt_0'
    # binning['ctrl_mm_tk_pt_0'] = [12, 0.5, 10]
    # ctrlVar['ctrl_mm_tk_pt_1'] = 'tkPt_1'
    # binning['ctrl_mm_tk_pt_1'] = [20, 0.5, 4]
    # ctrlVar['ctrl_p__tk_pt_0'] = 'tkPt_0'
    # binning['ctrl_p__tk_pt_0'] = [50, 0.5, 15]
    # ctrlVar['ctrl_pm_tk_pt_0'] = 'tkPt_0'
    # binning['ctrl_pm_tk_pt_0'] = [40, 0.5, 18]
    # ctrlVar['ctrl_pm_tk_pt_1'] = 'tkPt_1'
    # binning['ctrl_pm_tk_pt_1'] = [30, 0.5, 8]
    # ctrlVar['ctrl_pp_tk_pt_0'] = 'tkPt_0'
    # binning['ctrl_pp_tk_pt_0'] = [30, 0.5, 15]
    # ctrlVar['ctrl_pp_tk_pt_1'] = 'tkPt_1'
    # binning['ctrl_pp_tk_pt_1'] = [30, 0.5, 4]


    for s in ['m_', 'p_', 'mm', 'pm', 'pp']:
        # ctrlVar['ctrl_'+s+'_mu_pt'] = 'mu_pt'
        # binning['ctrl_'+s+'_mu_pt'] = binning['mu_pt'][0]
        if args.use_mva:
            ctrlVar['ctrl_'+s+'_MVA'] = 'MVA'
            binning['ctrl_'+s+'_MVA'] = binning['MVA']

        ctrlVar['ctrl_'+s+'_M2miss'] = 'M2_miss'
        binning['ctrl_'+s+'_M2miss'] = [18, np.min(dSet['mu']['M2_miss']), 7]

        ctrlVar['ctrl_'+s+'_q2'] = 'q2'
        binning['ctrl_'+s+'_q2'] = [30, np.min(dSet['mu']['q2']), np.max(dSet['mu']['q2'])]

    ctrlVar['ctrl_pm_dM_DstD'] = 'deltaM_DstD'
    binning['ctrl_pm_dM_DstD'] = [75, 0.1434, 0.1475]

    ctrlVar['ctrl_pm_mPiPi'] = 'massTks_pipi'
    binning['ctrl_pm_mPiPi'] = [30, 0.25, 1.]

    ctrlVar['ctrl_pm_mDstPi_0'] = 'tkMassHad_0'
    binning['ctrl_pm_mDstPi_0'] = [30, 2.1, 3.]

    ctrlVar['ctrl_pm_mu_iso'] = 'mu_db_iso04'
    binning['ctrl_pm_mu_iso'] = [50, 0, 80]

    # Figuring out the mod to avoid double counting
    ctrlVar_mod = defaultdict(lambda : None)
    ctrlVar_counter = defaultdict(lambda : 0)
    for r in args.control_regions:
        if not 'ctrl_'+r in ctrlVar.keys():
            raise Exception("[ERROR] Region '{}' not defined".format(r))

        ctrlVar_mod['ctrl_'+r] = [ -1, ctrlVar_counter[r[:2]] ]
        ctrlVar_counter[r[:2]]+= 1

    for k in ctrlVar_mod.keys():
        ctrlVar_mod[k][0] = ctrlVar_counter[k[5:7]]

    print '[DEBUG] Control regions modulos:', ','.join([k+':'+str(v) for k, v in ctrlVar_mod.iteritems()])

    print '---------> Fill control regions histograms'
    for k in ctrlVar.keys():
        histo[k] = {}

    totalCounting = {}
    for n in processOrder:
        ds = dSetTkSide[n]
        if n == 'data': continue
        print '\n----------->', n, '<-------------'
        wVar = {}
        weights = {}
        if 'data' not in n:
            weights['ctrl'], wVar['ctrlUp'], wVar['ctrlDown'] = get_ctrl_weights(ds)
        if n == 'dataSS_DstMu':
            nTotExp = ds['q2'].shape[0]
        else:
            sMC = MCsample[n]

            nGenExp = sMC.effMCgen['xsec'][0] * expectedLumi[category.name] * data_over_MC_overallNorm
            eff = [1, 0]
            for f, df in [sMC.effMCgen['effGEN'],
                          decayBR[n],
                          sMC.effCand['effCAND'],
                          sMC.getSkimEff(category.name+'_trkCtrl_'+mcType),
                         ]:
                eff[0] *= f
                eff[1] += np.square(df/f)
            eff[1] = eff[0] * np.sqrt(eff[1])
            if n+'_tk' in corrScaleFactors.keys():
                eff[0] *= corrScaleFactors[n+'_tk']
                print 'Using scale factor from a posteriori selection: {:.3f}'.format(corrScaleFactors[n+'_tk'])
            nTotExp = nGenExp*eff[0]

            print 'Including pileup reweighting'
            weights['pileup'] = puReweighter.getPileupWeights(ds['MC_nInteractions'])

            if args.beamspot_calibration:
                print 'Including beam spot correction'
                weights['beamSpot'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs')
                wVar[category.name+'BSyUp'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=0, dmu_y=8)/weights['beamSpot']
                wVar[category.name+'BSyDown'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=0, dmu_y=-8)/weights['beamSpot']
                wVar[category.name+'BSxUp'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=4, dmu_y=0)/weights['beamSpot']
                wVar[category.name+'BSxDown'] = getBeamSpotCorrectionWeights(ds, beamSpotParam, ref='bs', dmu_x=-4, dmu_y=0)/weights['beamSpot']

            print 'Including trigger corrections'
            nameSF = 'trg{}SF'.format(category.trg)
            weights[nameSF], wSfUp, wSfDw = computeTrgSF(ds, hTriggerSF)
            auxOnes = np.ones_like(wSfUp)
            # for i_eta in range(1, hTriggerSF.GetNbinsZ()+1):
            #     c_eta = hTriggerSF.GetZaxis().GetBinCenter(i_eta)
            #     w_eta = hTriggerSF.GetZaxis().GetBinWidth(i_eta)
            # for i_ip in range(1, hTriggerSF.GetNbinsY()+1):
            #     c_ip = hTriggerSF.GetYaxis().GetBinCenter(i_ip)
            #     w_ip = hTriggerSF.GetYaxis().GetBinWidth(i_ip)
            #     if c_ip + 0.5*w_ip <= category.minIP:
            #         continue
            for i_pt in range(1, hTriggerSF.GetNbinsX()+2):
                if i_pt > hTriggerSF.GetNbinsX() and category.name == 'High':
                    sel = ds['mu_pt'] > hTriggerSF.GetXaxis().GetXmax()
                else:
                    c_pt = hTriggerSF.GetXaxis().GetBinCenter(i_pt)
                    w_pt = hTriggerSF.GetXaxis().GetBinWidth(i_pt)
                    if (c_pt + 0.5*w_pt <= category.min_pt) or (c_pt - 0.5*w_pt >= category.max_pt):
                        continue

                    sel = np.abs(ds['mu_pt'] - c_pt) < w_pt
                # sel = np.logical_and(sel, np.abs(ds['mu_sigdxy'] - c_ip) < w_ip)
                # sel = np.logical_and(sel, np.abs(ds['mu_eta'] - c_eta) < w_eta)
                # binName = '_pt{}ip{}eta{}'.format(i_pt, i_ip, i_eta)
                # print 'Trg SF', i_pt, i_ip, i_eta, '-> selected {}'.format(np.sum(sel))
                # binName = '_pt{}ip{}'.format(i_pt, i_ip)
                binName = '_pt{}'.format(i_pt)
                wVar[nameSF+binName+'Up'] = np.where(sel, wSfUp, auxOnes)
                wVar[nameSF+binName+'Down'] = np.where(sel, wSfDw, auxOnes)

            print 'Including muon ID corrections'
            weights['muonIdSF'], _, _ = computeMuonIDSF(ds)

            print 'Including soft track pT corrections'
            partList = ['K_pt', 'pi_pt', 'pis_pt']
            for nBin in range(len(softPtUnc)):
                refPt = '{:.0f}'.format(np.round(np.mean(softPtUnc[nBin][:-1])*1e3))
                wVar['softTrkEff_'+refPt+'Up'] = weightsSoftTrackEff(ds, partList, bin=nBin, size=+1)
                wVar['softTrkEff_'+refPt+'Down'] = weightsSoftTrackEff(ds, partList, bin=nBin, size=-1)

            ############################
            # B phase space corrections
            ############################
            print 'Including B eta corrections'
            cname = 'B_eta'+category.name
            weights[cname], auxVarDic = computeKinCalWeights(ds, 'MC_B_eta', cname, cal_eta_B)
            wVar.update(auxVarDic)

            if args.cal_b_pt and n in samples_Bd:
                print 'Including Bd pT corrections'
                cname = 'BdpT'+category.name
                if cal_pT_Bd.kind == 'ratio':
                    weights[cname], wVar[cname+'Up'], wVar[cname+'Down'] = computeKinCalWeights(ds, 'MC_B_pt', None, cal_pT_Bd)
                else:
                    weights[cname], auxVarDic = computeKinCalWeights(ds, 'MC_B_pt', cname, cal_pT_Bd)
                    wVar.update(auxVarDic)

            # Correct the amount of random tracks from PV
            aux = '' if args.correlate_tk_pv_frac else category.name
            weights['randTksPV'], wVar['randTksPV'+aux+'Up'], wVar['randTksPV'+aux+'Down'] = computeRandomTracksWeights(ds, relScale=0.5, centralVal=1.3, kind='PV')
            print 'Average random tracks from PV weight: {:.2f}'.format(np.mean(weights['randTksPV']))
            weights['randTksPU'], wVar['randTksPU'+aux+'Up'], wVar['randTksPU'+aux+'Down'] = computeRandomTracksWeights(ds, relScale=0.5, centralVal=1.3, kind='PU')
            print 'Average random tracks from PU weight: {:.2f}'.format(np.mean(weights['randTksPU']))

            print 'Including additional tracks pT corrections'
            cname = 'addTk_pt_cali_'+category.name
            w, auxVarDic = computeKinCalWeights(ds, 'tkPt_0', cname, cal_addTK_pt)
            # Apply it only if they are not from main B
            orig = ds['ctrl'] == ds['ctrl2']
            sel = (ds['MC_tkFromMainB_0'] < 0.5) & orig
            print 'Affecting {:.1f}% of additional tracks'.format(100*np.sum(sel)/float(ds[orig].shape[0]))
            weights[cname] = np.where(sel, w * np.sum(sel) / np.sum(w[sel]), 1)
            print 'Normalization change: {:.3f}'.format(np.sum(weights[cname][orig])/ float(weights[cname][orig].shape[0]))
            for k in auxVarDic.keys():
                wVar[k] = np.where(sel, auxVarDic[k] *  np.sum(sel) / np.sum((weights[cname]*auxVarDic[k])[sel]), 1. )
            # Now, apply the same correction to duplicate tracks
            sel = (ds['MC_tkFromMainB_0'] < 0.5) & ~orig
            weights[cname] *= np.where(sel, w * np.sum(sel) / np.sum(w[sel]), 1)
            for k in auxVarDic.keys():
                wVar[k] *= np.where(sel, auxVarDic[k] *  np.sum(sel) / np.sum((weights[cname]*auxVarDic[k])[sel]), 1. )

        ############################
        # Form factor correction
        ############################
        if n in ['mu', 'tau'] and args.ff_scheme != 'NoFF':
            print 'Including FF corrections (Hammer)'
            weights['B2DstFF'] = (ds['wh_'+args.ff_scheme+'Central']*sMC.effCand['rate_den']/sMC.effCand['rate_'+args.ff_scheme+'Central']).values
            for nPar in FreeParFF[args.ff_scheme]:
                for var in ['Up', 'Down']:
                    tag = args.ff_scheme + nPar + var
                    wVar['B2Dst'+tag] = ds['wh_'+tag]/ds['wh_'+args.ff_scheme+'Central']
                    wVar['B2Dst'+tag] *= sMC.effCand['rate_'+args.ff_scheme+'Central']/sMC.effCand['rate_' + tag]
                    wVar['B2Dst'+tag][np.isnan(wVar['B2Dst'+tag])] = 0.


        if '_MuDstPi' in n:
            print 'Including B -> D**MuNu FF corrections (Hammer)'
            weights['purgeUwantedEvts'] = np.array(ds['procId_Dstst'] >= 0).astype(np.int)
            procId_Dstst = np.array(ds['procId_Dstst']).astype(np.int)

            ratesRatio = np.ones(50)
            ratesRatio[1] = sMC.effCand['rate_den_D1']/sMC.effCand['rate_D1BLR_central']
            ratesRatio[2] = sMC.effCand['rate_den_D1st']/sMC.effCand['rate_D1stBLR_central']
            ratesRatio[3] = sMC.effCand['rate_den_D2st']/sMC.effCand['rate_D2stBLR_central']

            number_invalid = np.count_nonzero(ds['procId_Dstst'] < 0)
            if number_invalid > 0:
                print_warning("Warning: There are %i events in the B -> Mu D* pi sample with a process id of less than zero!" % number_invalid)

                # ratesRatio doesn't have an index for unknown events with
                # procId_Dstst of -100. These events get a weight of 0 anyways,
                # so we just set the FF weight to 1 to prevent an error.
                # FIXME: Is there a better way to do this?
                procId_Dstst[procId_Dstst < 0] = len(ratesRatio) - 1

            selDstst = np.logical_and(procId_Dstst >= 1, procId_Dstst <= 3)
            weights['B2DststFF'] = np.where(selDstst, ds['wh_Dstst_BLRCentral']*ratesRatio[procId_Dstst], 1.)

            selDstst = np.logical_or(procId_Dstst == 1, procId_Dstst == 3)
            for nEig in [1,2,3,4]:
                for var in ['Up', 'Down']:
                    tag = 'DststN_BLReig'+str(nEig)+var
                    ratesRatio = np.ones(50)
                    ratesRatio[1] = sMC.effCand['rate_D1BLR_central']/sMC.effCand['rate_D1BLR_eig'+str(nEig)+var]
                    ratesRatio[3] = sMC.effCand['rate_D2stBLR_central']/sMC.effCand['rate_D2stBLR_eig'+str(nEig)+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_Dstst_BLRCentral'])*ratesRatio[procId_Dstst], 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.

            selDstst = procId_Dstst == 2
            for nEig in [1,2,3]:
                for var in ['Up', 'Down']:
                    tag = 'DststW_BLReig'+str(nEig)+var
                    ratesRatio = sMC.effCand['rate_D1stBLR_central']/sMC.effCand['rate_D1stBLR_eig'+str(nEig)+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_Dstst_BLRCentral'])*ratesRatio, 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.

        if n.endswith('_MuDstPiPi') and not args.skip_blop:
            print 'Including B -> D(2S)MuNu FF corrections (Hammer)'
            procId_Dstst = np.array(ds['procId_Dstst']).astype(np.int)

            ratesRatio = np.ones(50)
            ratesRatio[4] = sMC.effCand['rate_den_D2sst']/sMC.effCand['rate_D2sstBLOP_central']
            ratesRatio[11] = ratesRatio[4]
            ratesRatio[12] = ratesRatio[4]
            ratesRatio[13] = ratesRatio[4]
            ratesRatio[23] = sMC.effCand['rate_den_D2s']/sMC.effCand['rate_D2sBLOP_central']

            selDstst = np.logical_and(procId_Dstst >= 4, procId_Dstst <= 23)
            weights['B2D2S_FF'] = np.where(selDstst, ds['wh_D2S_BLOPCentral']*ratesRatio[procId_Dstst], 1.)

            for parName in ['RhoSq', 'chi11', 'chi21', 'chi31', 'eta1']:
                for var in ['Up', 'Down']:
                    tag = 'D2S_BLOP'+parName+var
                    ratesRatio = np.ones(50)
                    ratesRatio[4] = sMC.effCand['rate_D2sstBLOP_central']/sMC.effCand['rate_D2sstBLOP_'+parName+var]
                    ratesRatio[11] = ratesRatio[4]
                    ratesRatio[12] = ratesRatio[4]
                    ratesRatio[13] = ratesRatio[4]
                    ratesRatio[23] = sMC.effCand['rate_D2sBLOP_central']/sMC.effCand['rate_D2sBLOP_'+parName+var]
                    wVar['FF_B2'+tag] = np.where(selDstst, (ds['wh_'+tag]/ds['wh_D2S_BLOPCentral'])*ratesRatio[procId_Dstst], 1.)
                    wVar['FF_B2'+tag][np.isnan(wVar['FF_B2'+tag])] = 0.

        ############################
        # Cleaning signal form slipped in events
        ############################
        if n in ['mu', 'tau']:
            weights['kill_slipped_in'] = np.where( ds['procId_signal'] == {'tau': 0, 'mu': 1}[n], 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

        ############################
        # Dstst resonance mix
        ############################
        if re.match('B[du]_MuDstPi\Z', n):
            print 'Including D**->D*Pi branching ratio and width variations'

            weights['kill_slipped_in'] = np.where( np.logical_and(ds['procId_Dstst'] >= 0, ds['procId_Dstst'] <= 3), 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstPi_mix:
                wc, wu, wd = computeBrVarWeights(ds, {'procId_Dstst': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'brB_DstPiMuNu_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd


            nn = 'D2420_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[10423, 2.421, 0.0274], [10413, 2.423, 0.020]],
                                                                                 newGamma=[0.030, 0.025], relScale=0.15)

            nn = 'D2430_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[20423, 2.445, 0.250], [20413, 2.445, 0.250]],
                                                                                 newGamma=[0.3, 0.3], relScale=0.1)

            nn = 'D2460_width'
            weights[nn], wVar[nn+'Up'], wVar[nn+'Down'] = computeWidthVarWeights(ds, selItems=[[425, 2.461, 0.049], [415, 2.460, 0.037]],
                                                                                 newGamma=[0.045, 0.045], relScale=0.15)

        if not re.search('MuDstPiPi\Z', n) is None:
            print 'Including B -> MuNuD*PiPi width and Br variations'
            widthMods = [[x, 2.640, 0.300] for x in [100413, 100423, 100411, 100421]]
            uncN = 'Dst2S_width'
            weights[uncN], wVar[uncN+'Up'], wVar[uncN+'Down'] = computeWidthVarWeights(ds, selItems=widthMods, relScale=0.3)

            weights['kill_slipped_in'] = np.where( ds['procId_Dstst'] >= 0, 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstPiPi_mix:
                wc, wu, wd = computeBrVarWeights(ds, {'procId_Dstst': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'brB_DstPiPiMuNu_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd

        ############################
        # Hc mix variations
        ############################
        if n in DstHc_sample_id.keys():
            print 'Including', n, 'mix variations'
            weights['kill_slipped_in'] = np.where( np.floor_divide(ds['procId_DstHc'], 100) == DstHc_sample_id[n], 1, 0)
            print 'Surviving after killing slipped in {:.3f}%'.format(100*np.sum(weights['kill_slipped_in'])/float(ds.shape[0]))

            for proc_id, centralVal, relScale, inflateRate in uncertainties_DstHc_mix:
                if np.floor_divide(proc_id, 100) != DstHc_sample_id[n]: continue
                wc, wu, wd = computeBrVarWeights(ds, {'procId_DstHc': proc_id}, centralVal=centralVal, relScale=inflateRate*relScale, absVal=False)
                name = 'br'+n+'_'+str(proc_id)
                weights[name], wVar[name+'Up'], wVar[name+'Down'] = wc, wu, wd

        if n == 'B_DstDXX':
            nnn = 'Bu_DstDXX_frac'
            weights[nnn], wVar[nnn+'Up'], wVar[nnn+'Down'] = computeBrVarWeights(ds, {'MC_DstMotherPdgId': 521}, relScale=0.5, centralVal=2., keepNorm=True)


        ############################
        # Creating histograms
        ############################

        print 'Computing total weights'
        weightsCentral = np.ones_like(ds['q2'])
        for w in weights.values():
            weightsCentral *= w
        wVar[''] = np.ones_like(weightsCentral)

        if args.dump_weights_tree and not 'data' in n:
            weightsDir = os.path.join(MCsample[n].skimmed_dir, 'weights')
            if not os.path.isdir(weightsDir):
                os.makedirs(weightsDir)

            mcType = 'bare' if args.bare_mc else 'corr'
            auxName = category.name + 'trkCtrl_' + mcType + '_' + card_name + '.root'

            orig = ds['ctrl'] == ds['ctrl2']
            wDf = pd.DataFrame.from_dict({'central': weightsCentral*nTotExp/float(weightsCentral[orig].shape[0]) })
            for k, w in wVar.iteritems():
                if k:
                    wDf[k] = w*wDf['central']

            print 'Dumping weights tree in', os.path.join(weightsDir, auxName)
            rtnp.array2root(wDf.to_records(), os.path.join(weightsDir, auxName), treename='weights', mode='RECREATE')

        sel = {}
        scale = {}
        latexTableString = {}
        table = PrettyTable()
        table.field_names = ['Region', 'Selected', 'Exp. bare', 'Exp. weights']
        for k, selFun in controlRegSel.iteritems():
            if n == 'dataSS_DstMu' and k == 'mm':
                selFun = controlRegSel['pm']
            elif n == 'dataSS_DstMu' and k == 'pm':
                selFun = controlRegSel['mm']
            sel[k] = selFun(ds)
            orig = ds['ctrl'] == ds['ctrl2']
            if 'data' not in n:
                sel[k] &= orig
            nTotSel = float(np.sum(sel[k]))
            nExp = nTotExp * nTotSel / sel[k][orig].shape[0]
            # if n == 'dataSS_DstMu' and k == 'mm':
            #     nExp *= 1e-3
            nAux = nTotExp * np.sum(weightsCentral[sel[k]]) / sel[k][orig].shape[0]
            table.add_row([k] + '{:.0f} {:.0f} {:.0f}'.format(nTotSel, nExp, nAux).split(' '))
            latexTableString[k] = '{:.0f} ({:.0f})'.format(nAux, nTotSel)
            if not k in totalCounting.keys():
                totalCounting[k] = [0, 0]
            totalCounting[k][0] += nAux
            totalCounting[k][1] += nTotSel
            if nTotSel ==0:
                nTotSel+=1
            scale[k] = nExp/nTotSel
        print table.get_string(sortby="Region")
        s = ' & '.join([latexTableString[s] for s in ['m_', 'p_', 'mm', 'pm', 'pp']])
        print s
        eventCountingStr[n] += ' & ' + s + '\\\\'

        new_histo = histogram_ctrl(ds, n, binning, weightsCentral, wVar, sel, ctrlVar, ctrlVar_mod, scale)

        histo = update(histo,new_histo)

    if args.dump_weights_tree:
        print 'Exiting runCombine.py. To run further remove dump_weights_tree option.'
        exit()

    s = ' & '.join(['{:.0f} ({:.0f})'.format(*totalCounting[s]) for s in ['p_', 'm_', 'mm', 'pm', 'pp']]) + ' \\\\'
    eventCountingStr['tot'] += ' & ' + s
    with open(outdir + '/eventCounting.txt', 'w') as f:
        for p in processOrder + ['tot']:
            f.write(p + '   ' + eventCountingStr[p] + '\n')

    ######################################################
    ########## Create total MC histograms and Pseudo data if necessary
    ######################################################
    print 'Creating total MC'
    for k, hDic in histo.iteritems():
        h = hDic.values()[0].Clone('total')
        h.SetTitle('Total MC')
        h.Reset()
        for n, hMC in hDic.iteritems():
            if not '__' in n and not n == 'data':
                scale = SM_RDst if 'tau' in n else 1.
                h.Add(hMC, scale)
        histo[k]['total'] = h
        if args.asimov:
            hAsimov = h.Clone('data_obs')
            hAsimov.Sumw2(0)
            for ix in range(1, hAsimov.GetNbinsX()+1):
                if hAsimov.GetNbinsY() > 1:
                    for iy in range(1, hAsimov.GetNbinsY()+1):
                        hAsimov.SetBinContent(ix, iy, np.around(hAsimov.GetBinContent(ix, iy)))
                else:
                    hAsimov.SetBinContent(ix, np.around(hAsimov.GetBinContent(ix)))
            hAsimov.Sumw2()
            histo[k]['data'] = hAsimov

    ######################################################
    ########## Create Real data Histograms
    ######################################################
    if not args.asimov:
        print 'Creating data histos'
        ds = dSet['data']
        print 'N observed data signal region: {:.1f}k'.format(1e-3*ds['q2'].shape[0])

        for i_q2 in range(len(binning['q2'])-1):
            q2_l = binning['q2'][i_q2]
            q2_h = binning['q2'][i_q2 + 1]
            sel_q2 = np.logical_and(ds['q2'] > q2_l, ds['q2'] < q2_h)
            for var in observables_q2bins:
                cat_name = var+'_q2bin'+str(i_q2)
                histo[cat_name]['data'] = create_TH1D(
                                                      ds[var][sel_q2],
                                                      name='data_obs', title='Data Obs',
                                                      binning=binning[var][i_q2],
                                                      opt='underflow,overflow'
                                                     )
                if var == 'M2_miss':
                    auxS = np.column_stack((ds['M2_miss'][sel_q2], ds['Est_mu'][sel_q2]))
                    cat2D = 'h2D_q2bin'+str(i_q2)
                    histo[cat2D]['data'] = create_TH2D(auxS, name='data_obs', title='Data Obs',
                                                       binning=binning_2D[i_q2])
                    catU = 'Unrolled_q2bin'+str(i_q2)
                    validBins = unrolledBins[i_q2]
                    hUnrolled = rt.TH1D('data_obs', 'Data Obs', len(validBins), 0.5, len(validBins)+0.5)
                    for i, (ix, iy) in enumerate(validBins):
                        hUnrolled.SetBinContent(i+1, histo[cat2D]['data'].GetBinContent(ix, iy))
                        hUnrolled.SetBinError(i+1, histo[cat2D]['data'].GetBinError(ix, iy))
                    histo[catU]['data'] = hUnrolled

        for var in observables_q2integrated:
            varName = var if not var == 'specQ2' else 'q2'
            histo[var]['data'] = create_TH1D(ds[varName], name='data_obs', binning=binning[var], opt='underflow,overflow')

        ds = dSetTkSide['data']
        for k, var in ctrlVar.iteritems():
            region = k[5:7]
            auxSel = controlRegSel[region](ds)
            if not ctrlVar_mod[k] is None:
                m, j = ctrlVar_mod[k]
                if m > 1:
                    auxSel = np.logical_and( np.mod(ds['index'], m) == j, auxSel )

            histo[k]['data'] = create_TH1D(ds[var][auxSel],
                                           name='data_obs', title='Data Obs',
                                           binning=binning[k],
                                           opt='',
                                          )

        print 'N observed data control regions: ' + ' & '.join(['{:.0f}'.format(histo['ctrl_'+s+'_mHad']['data'].Integral()) for s in ['p_', 'm_', 'mm', 'pm', 'pp']]) + ' \\\\'

    ######################################################
    ########## Dump root file
    ######################################################
    print 'Dumping histos in root files'
    if not os.path.isdir(histo_file_dir): os.makedirs(histo_file_dir)

    for cat_name, h_dic in histo.iteritems():
        tf = rt.TFile(histo_file_dir+'{}_{}.root'.format(card_name, cat_name), 'recreate')
        for v in h_dic.values():
            v.Write()
        tf.Close()
    cmd = 'date > ' +histo_file_dir+'histos_{}_ready.log'.format(card_name)
    os.system(cmd)


########################### -------- Create histrograms ------------------ #########################

def drawPlots(tag, hDic, catName, args, scale_dic=None):
    if scale_dic is None:
        scale_dic = {}
    print 20*'-', 'Drawing plots', tag, 20*'-'
    outCanvas = []
    CMS_lumi.integrated_lumi = expectedLumi[catName]

    print 'Drawing signal region grid'
    cAux = plot_gridVarQ2(CMS_lumi, binning, hDic, draw_pulls=True,
                          pullsRatio=True, pulls_ylim=[0.85, 1.1],
                          scale_dic=scale_dic,
                          categoryText=catName, cNameTag=tag,
                          iq2_maskData=[] if args.unblinded else [2, 3])
    cAux.SaveAs(webFolder+'/signalRegion_'+tag+'.png')
    outCanvas.append(cAux)

    if 'dxy_vtxD0pismu_PV' in hDic.keys():
        print 'Drawing dxy_vtxD0pismu_PV'
        hDic['dxy_vtxD0pismu_PV']['data'].GetXaxis().SetTitle('dxy_vtxD0pismu_PV')
        hDic['dxy_vtxD0pismu_PV']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['dxy_vtxD0pismu_PV'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'dxy_vtxD0pismu_PV', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/dxy_vtxD0pismu_PV_'+tag+'.png')
        outCanvas.append(cAux)

    if 'sigdxy_vtxD0_BS' in hDic.keys():
        print 'Drawing sigdxy_vtxD0_BS'
        hDic['sigdxy_vtxD0_BS']['data'].GetXaxis().SetTitle('sigdxy_vtxD0_BS')
        hDic['sigdxy_vtxD0_BS']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['sigdxy_vtxD0_BS'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'sigdxy_vtxD0_BS', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/sigdxy_vtxD0_BS_'+tag+'.png')
        outCanvas.append(cAux)

    if 'PV_ndof' in hDic.keys():
        print 'Drawing PV_ndof'
        hDic['PV_ndof']['data'].GetXaxis().SetTitle('PV ndof')
        hDic['PV_ndof']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['PV_ndof'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'PV_ndof', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/PV_ndof_'+tag+'.png')
        outCanvas.append(cAux)

    if 'PV_chi2' in hDic.keys():
        print 'Drawing PV_chi2'
        hDic['PV_chi2']['data'].GetXaxis().SetTitle('PV chi2')
        hDic['PV_chi2']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['PV_chi2'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'PV_chi2', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/PV_chi2_'+tag+'.png')
        outCanvas.append(cAux)

    if 'PV_pval' in hDic.keys():
        print 'Drawing PV_pval'
        hDic['PV_pval']['data'].GetXaxis().SetTitle('PV p-value')
        hDic['PV_pval']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['PV_pval'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'PV_pval', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/PV_pval_'+tag+'.png')
        outCanvas.append(cAux)

    if 'pval_piK' in hDic.keys():
        print 'Drawing pval_piK'
        hDic['pval_piK']['data'].GetXaxis().SetTitle('piK p-value')
        hDic['pval_piK']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['pval_piK'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'pval_piK', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/pval_piK_'+tag+'.png')
        outCanvas.append(cAux)

    if 'pval_D0pis' in hDic.keys():
        print 'Drawing pval_D0pis'
        hDic['pval_D0pis']['data'].GetXaxis().SetTitle('D0pis p-value')
        hDic['pval_D0pis']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['pval_D0pis'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'pval_D0pis', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/pval_D0pis_'+tag+'.png')
        outCanvas.append(cAux)

    if 'pval_D0pismu' in hDic.keys():
        print 'Drawing pval_D0pismu'
        hDic['pval_D0pismu']['data'].GetXaxis().SetTitle('D0pismu p-value')
        hDic['pval_D0pismu']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['pval_D0pismu'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'pval_D0pismu', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/pval_D0pismu_'+tag+'.png')
        outCanvas.append(cAux)

    if 'B_pt' in hDic.keys():
        print 'Drawing B_pt'
        hDic['B_pt']['data'].GetXaxis().SetTitle('B p_{T} [GeV]')
        hDic['B_pt']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['B_pt'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'B_pt', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/B_pt_'+tag+'.png')
        outCanvas.append(cAux)

        hDic['B_pt']['data'].GetYaxis().SetTitle('Normalized events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['B_pt'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   density=True,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=0, tag=tag+'B_pt', legLoc=[0.65, 0.4, 0.9, 0.75])
        cAux.SaveAs(webFolder+'/B_pt_norm_'+tag+'.png')
        outCanvas.append(cAux)

    if 'specQ2' in hDic.keys():
        print 'Drawing q2 spectrum'
        hDic['specQ2']['data'].GetXaxis().SetTitle('q^{2} [GeV]')
        hDic['specQ2']['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['specQ2'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=1, tag=tag+'specQ2', legLoc=[0.75, 0.4, 0.93, 0.75])
        cAux.SaveAs(webFolder+'/q2_'+tag+'.png')
        outCanvas.append(cAux)

        hDic['specQ2']['data'].GetYaxis().SetTitle('Normalized events')
        cAux = plot_SingleCategory(CMS_lumi, hDic['specQ2'], draw_pulls=True, pullsRatio=True, scale_dic=scale_dic,
                                   density=True,
                                   addText='Cat. '+catName, logy=False, legBkg=True,
                                   min_y=0, tag=tag+'specQ2', legLoc=[0.75, 0.4, 0.93, 0.75])
        cAux.SaveAs(webFolder+'/q2_norm_'+tag+'.png')
        outCanvas.append(cAux)

    varsToPlot = []
    for nn in hDic.keys():
        if nn in ['B_pt', 'specQ2']:
            continue
        if '_q2bin' in nn:
            continue
        varsToPlot.append(nn)
    varsToPlot = sorted(varsToPlot)
    for var in varsToPlot:
        print 'Drawing '+var
        title = var[8:] if var.startswith('ctrl_') else var
        title = title.replace('_eta', ' #eta').replace('st', '*').replace('_', ' ')
        hDic[var]['data'].GetXaxis().SetTitle(title)
        hDic[var]['data'].GetYaxis().SetTitle('Events')
        ctrl_text = (', '+getControlSideText(var)) if var.startswith('ctrl_') else ''
        cAux = plot_SingleCategory(CMS_lumi, hDic[var],
                                   draw_pulls=True, pullsRatio=True, pulls_ylim='auto',
                                   scale_dic=scale_dic,
                                   addText='Cat. '+catName+ctrl_text,
                                   logy=False, legBkg=True,
                                   min_y=0, tag=tag+var, legLoc=[0.77, 0.55, 0.94, 0.75])
        cAux.SaveAs(webFolder+'/'+var+'_'+tag+'.png')
        outCanvas.append(cAux)

        # if var.endswith('eta'):
        #     h = hDic[var]['data']
        #     nbins = h.GetNbinsX()
        #     xmin = h.GetBinCenter(1) - 0.5*h.GetBinWidth(1)
        #     xmax = h.GetBinCenter(nbins) + 0.5*h.GetBinWidth(nbins)
        #     if nbins%2 != 0 or np.abs(xmax+xmin) > 1e-3:
        #         continue
        #     title = title.replace('#eta', '|#eta|')
        #     hDicAux = {}
        #     for k, h in hDic[var].iteritems():
        #         hDicAux[k] = rt.TH1D(h.GetName()+'abs', h.GetTitle(), int(nbins/2), 0, xmax)
        #         for ib in range(1, int(nbins/2)+1):
        #             c = h.GetBinContent(int(nbins/2) + ib) + h.GetBinContent(int(nbins/2) + 1 - ib)
        #             dc = np.hypot(h.GetBinError(int(nbins/2) + ib), h.GetBinError(int(nbins/2) + 1 - ib))
        #             hDicAux[k].SetBinContent(ib, c)
        #             hDicAux[k].SetBinError(ib, dc)
        #     hDicAux['data'].GetXaxis().SetTitle(title)
        #     hDicAux['data'].GetYaxis().SetTitle('Events')
        #     cAux = plot_SingleCategory(CMS_lumi, hDicAux,
        #                                draw_pulls=True, pullsRatio=True, pulls_ylim='auto',
        #                                scale_dic=scale_dic,
        #                                addText='Cat. '+catName, logy=False, legBkg=True,
        #                                min_y=1, tag=tag+var, legLoc=[0.77, 0.55, 0.94, 0.75])
        #     cAux.SaveAs(webFolder+'/'+var+'abs_'+tag+'.png')
        #     outCanvas.append(cAux)



    # 2D plot
    # for i_q2 in range(len(binning['q2'])-1):
    #     name2D = 'h2D_q2bin'+str(i_q2)
    #     if not name2D in hDic.keys():
    #         continue
    #     print 'Drawing', name2D
    #     hSum = hDic[name2D]['total']
    #     hSum.GetXaxis().SetTitle('m^{2}_{miss} [GeV^{2}]')
    #     hSum.GetYaxis().SetTitle('E_{#mu}* [GeV]')
    #     hSum.GetZaxis().SetTitle('Events')
    #     cAux = drawOnCMSCanvas(CMS_lumi, [hSum], ['colz'], tag=str(i_q2), mR=0.17)
    #     l = rt.TLatex()
    #     l.SetTextAlign(11)
    #     l.SetTextSize(0.05)
    #     l.SetTextFont(42)
    #     l.DrawLatexNDC(0.17, 0.8, 'Cat. '+catName)
    #     cAux.SetLogz()
    #     cAux.SaveAs(webFolder+'/M2Miss_vs_EstMu_q2bin{}_{}_TotMC.png'.format(i_q2, tag))
    #     outCanvas.append(cAux)

    #Re-rolling histos
    unrolledBins = None
    if args.category == 'comb':
        with open(outdir.replace('comb', catName.lower())+'/unrolledBinsMap.pkl', 'rb') as f:
            unrolledBins = pickle.load(f)
    else:
        with open(outdir+'/unrolledBinsMap.pkl', 'rb') as f:
            unrolledBins = pickle.load(f)
    hDic_reRollProj = {}
    for i_q2 in range(len(binning['q2'])-1):
        name2D = 'h2D_q2bin'+str(i_q2)
        nameU = 'Unrolled_q2bin'+str(i_q2)
        name2Dr = 'h2Dr_q2bin'+str(i_q2)
        hDic[name2Dr] = {}
        hDic_reRollProj['Est_mu_q2bin'+str(i_q2)] = {}
        hDic_reRollProj['M2_miss_q2bin'+str(i_q2)] = {}
        for n in hDic[nameU]:
            hDic[name2Dr][n] = hDic[name2D][n].Clone()
            hDic[name2Dr][n].Reset()
            for idx, (ix, iy) in enumerate(unrolledBins[i_q2]):
                hDic[name2Dr][n].SetBinContent(ix, iy, hDic[nameU][n].GetBinContent(idx+1))
                hDic[name2Dr][n].SetBinError(ix, iy, hDic[nameU][n].GetBinError(idx+1))
            # Re-rolled projections
            auxN = 'Est_mu_q2bin'+str(i_q2)
            hDic_reRollProj[auxN][n] = hDic[name2Dr][n].ProjectionY('hre_'+n+auxN, 1, -1, 'e')
            auxN = 'M2_miss_q2bin'+str(i_q2)
            hDic_reRollProj[auxN][n] = hDic[name2Dr][n].ProjectionX('hre_'+n+auxN, 1, -1, 'e')


    print 'Drawing rerolled 2D:',
    for i_q2 in range(len(binning['q2'])-1):
        print str(i_q2),
        name2D = 'h2Dr_q2bin'+str(i_q2)
        hSum = hDic[name2D]['total']
        hSum.GetXaxis().SetTitle('m^{2}_{miss} [GeV^{2}]')
        hSum.GetYaxis().SetTitle('E_{#mu}* [GeV]')
        hSum.GetZaxis().SetTitle('Events')
        cAux = drawOnCMSCanvas(CMS_lumi, [hSum], ['colz'], tag=str(i_q2), mR=0.17)
        l = rt.TLatex()
        l.SetTextAlign(11)
        l.SetTextSize(0.05)
        l.SetTextFont(42)
        l.DrawLatexNDC(0.17, 0.8, 'Cat. '+catName)
        cAux.SetLogz()
        cAux.SaveAs(webFolder+'/rerolled_M2Miss_vs_EstMu_q2bin{}_{}_TotMC.png'.format(i_q2, tag))
        outCanvas.append(cAux)
    print '\nCreating rerolled projections grid'
    cAux = plot_gridVarQ2(CMS_lumi, binning, hDic_reRollProj, draw_pulls=True,
                          pullsRatio=False, pulls_ylim=[0.9, 1.1],
                          scale_dic=scale_dic,
                          categoryText=catName, cNameTag=tag+'_rerolled',
                          iq2_maskData=[] if args.unblinded else [2, 3])
    cAux.SaveAs(webFolder+'/rerolled_signalRegion_'+tag+'.png')
    outCanvas.append(cAux)

    # Draw observables in bins of q2
    varsToPlot = []
    for nn in hDic.keys():
        if not '_q2bin' in nn:
            continue
        nn = nn[:nn.find('_q2bin')]
        if nn in ['M2_miss', 'Est_mu', 'h2D', 'h2Dr']:
            continue
        if nn in varsToPlot:
            continue
        varsToPlot.append(nn)
    varsToPlot = sorted(varsToPlot)
    varsToResumOver_q2 = ['mass_D0pismu']

    for var in varsToPlot:
        h_all = {}
        print 'Drawing', var, 'in q2 bins:',
        for i_q2 in range(len(binning['q2'])-1):
            print str(i_q2),
            if i_q2 == len(binning['q2'])-2:
                print '\n',
            q2_l = binning['q2'][i_q2]
            q2_h = binning['q2'][i_q2 + 1]
            nameFull = var+'_q2bin'+str(i_q2)

            if var == 'Unrolled':
                xtitle = 'Unrolled bin number'
            else:
                xtitle = var.replace('_', ' ').replace('pt', 'p_{T}').replace('mu', '#mu')
                if ('pt' in var) or ('mass' in var):
                    xtitle += ' [GeV]'
            hDic[nameFull]['data'].GetXaxis().SetTitle(xtitle)
            hDic[nameFull]['data'].GetYaxis().SetTitle('Events')

            pullsRatio = False if var in ['Unrolled', 'mass_D0pismu'] else True
            logy = True if var == 'Unrolled' else False
            min_y = 1 if logy else 0
            legLoc = [0.7, 0.4, 0.9, 0.75]
            figsize = [600, 450]
            if var == 'Unrolled':
                legLoc = [0.15, 0.5, 0.25, 0.8]
                figsize = [900, 400]
            if var == 'mass_D0pismu':
                legLoc = [0.16, 0.45, 0.33, 0.8]

            cAux = plot_SingleCategory(CMS_lumi, hDic[nameFull], scale_dic=scale_dic,
                                       maskData = (not args.unblinded) and (False if i_q2 < 2 else True),
                                       draw_pulls=True, pullsRatio=pullsRatio,
                                       logy=logy, min_y=min_y, legBkg=True,
                                       addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
                                       tag=tag+nameFull,
                                       legLoc=legLoc,
                                       figsize = figsize
                                       )
            cAux.SaveAs(webFolder+'/'+nameFull+'_'+tag+'.png')
            outCanvas.append(cAux)

            if var in varsToResumOver_q2:
                if i_q2 == 0:
                    for nn in hDic[nameFull].keys():
                        h_all[nn] = hDic[nameFull][nn].Clone()
                else:
                    for nn in h_all.keys():
                        h_all[nn].Add(hDic[nameFull][nn])

        if var in varsToResumOver_q2:
            print 'Drawing', var, 'normalized and summed over q2'
            h_all['data'].GetYaxis().SetTitle('Normalized events')
            cAux = plot_SingleCategory(CMS_lumi, h_all, scale_dic=scale_dic,
                                       draw_pulls=True, pullsRatio=True,
                                       addText='Cat. '+catName,
                                       logy=False, legBkg=True,
                                       min_y=0,
                                       max_y='data',
                                       pulls_ylim=[0.85, 1.15],
                                       density=True,
                                       tag=tag+var+'_all',
                                       legLoc=[0.2, 0.45, 0.4, 0.8],
                                       maskData = False
                                       )
            cAux.SaveAs(webFolder+'/'+var+'_allNorm_'+tag+'.png')
            outCanvas.append(cAux)

    # Draw unrolled histograms
    # for i_q2 in range(len(binning['q2'])-1):
    #     q2_l = binning['q2'][i_q2]
    #     q2_h = binning['q2'][i_q2 + 1]
    #     nameU = 'Unrolled_q2bin'+str(i_q2)
    #     if not nameU in hDic.keys():
    #         print nameU, 'not found'
    #         continue
    #     print 'Creating', nameU
    #     hDic[nameU]['data'].GetXaxis().SetTitle('Unrolled 2D bins')
    #     hDic[nameU]['data'].GetYaxis().SetTitle('Events')
    #     cAux = plot_SingleCategory(CMS_lumi, hDic[nameU], scale_dic=scale_dic,
    #                                draw_pulls=True, pullsRatio=False,
    #                                addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
    #                                logy=True, legBkg=True,
    #                                min_y=1,
    #                                tag=tag+'Unrolled_q2bin'+str(i_q2),
    #                                legLoc=[0.15, 0.5, 0.25, 0.8],
    #                                maskData = (not args.unblinded) and (False if i_q2 < 2 else True),
    #                                figsize = [900, 400]
    #                                )
    #     cAux.SaveAs(webFolder+'/Unrolled_q2bin'+str(i_q2)+'_'+tag+'.png')
    #     outCanvas.append(cAux)
    #
    # hMuPt_all = None
    # for i_q2 in range(len(binning['q2'])-1):
    #     q2_l = binning['q2'][i_q2]
    #     q2_h = binning['q2'][i_q2 + 1]
    #     name = 'mu_pt_q2bin'+str(i_q2)
    #     if not name in hDic.keys(): continue
    #     print 'Creating', name
    #     hDic[name]['data'].GetXaxis().SetTitle('#mu p_{T} [GeV]')
    #     hDic[name]['data'].GetYaxis().SetTitle('Events')
    #     cAux = plot_SingleCategory(CMS_lumi, hDic[name], scale_dic=scale_dic,
    #                                draw_pulls=True, pullsRatio=True,
    #                                addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
    #                                logy=False, legBkg=True,
    #                                min_y=1,
    #                                tag=tag+'mu_pt_q2bin'+str(i_q2),
    #                                legLoc=[0.7, 0.5, 0.9, 0.75],
    #                                maskData = (not args.unblinded) and (False if i_q2 < 2 else True)
    #                                )
    #     cAux.SaveAs(webFolder+'/muPt_q2bin'+str(i_q2)+'_'+tag+'.png')
    #     outCanvas.append(cAux)
    #     if i_q2 == 0:
    #         hMuPt_all = {}
    #         for n in hDic[name].keys():
    #             hMuPt_all[n] = hDic[name][n].Clone()
    #     else:
    #         for n in hMuPt_all.keys():
    #             hMuPt_all[n].Add(hDic[name][n])
    #
    # print 'Creating mu_pt_all'
    # hMuPt_all['data'].GetYaxis().SetTitle('Normalized events')
    # cAux = plot_SingleCategory(CMS_lumi, hMuPt_all, scale_dic=scale_dic,
    #                            draw_pulls=True, pullsRatio=True,
    #                            addText='Cat. '+catName,
    #                            logy=False, legBkg=True,
    #                            min_y=0,
    #                            max_y='data',
    #                            pulls_ylim=[0.85, 1.15],
    #                            density=True,
    #                            tag=tag+'mu_pt_all',
    #                            legLoc=[0.7, 0.3, 0.9, 0.55],
    #                            maskData = False
    #                            )
    # cAux.SaveAs(webFolder+'/muPt_allNorm_'+tag+'.png')
    # outCanvas.append(cAux)
    #
    # axName = {'Dst': 'D*', 'K':'K', 'pi':'#pi', 'pis':'#pi_{soft}'}
    # for n in ['Dst', 'K', 'pi', 'pis']:
    #     h_all = None
    #     for i_q2 in range(len(binning['q2'])-1):
    #         q2_l = binning['q2'][i_q2]
    #         q2_h = binning['q2'][i_q2 + 1]
    #         name = n+'_pt_q2bin'+str(i_q2)
    #         if not name in hDic.keys(): continue
    #         print 'Creating', name
    #         hDic[name]['data'].GetXaxis().SetTitle(axName[n] + ' p_{T} [GeV]')
    #         hDic[name]['data'].GetYaxis().SetTitle('Events')
    #         cAux = plot_SingleCategory(CMS_lumi, hDic[name], scale_dic=scale_dic,
    #                                    draw_pulls=True, pullsRatio=False,
    #                                    addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
    #                                    logy=False, legBkg=True,
    #                                    min_y=1,
    #                                    tag=tag+n+'_pt_q2bin'+str(i_q2),
    #                                    legLoc=[0.7, 0.4, 0.9, 0.75],
    #                                    maskData = (not args.unblinded) and (False if i_q2 < 2 else True)
    #                                    )
    #         cAux.SaveAs(webFolder+'/'+n+'Pt_q2bin'+str(i_q2)+'_'+tag+'.png')
    #         outCanvas.append(cAux)
    #         if i_q2 == 0:
    #             h_all = {}
    #             for nn in hDic[name].keys():
    #                 h_all[nn] = hDic[name][nn].Clone()
    #         else:
    #             for nn in h_all.keys():
    #                 h_all[nn].Add(hDic[name][nn])
    #     print 'Creating '+n+'Pt_all'
    #     h_all['data'].GetYaxis().SetTitle('Normalized events')
    #     cAux = plot_SingleCategory(CMS_lumi, h_all, scale_dic=scale_dic,
    #                                draw_pulls=True, pullsRatio=True,
    #                                addText='Cat. '+catName,
    #                                logy=False, legBkg=True,
    #                                min_y=0,
    #                                max_y='data',
    #                                pulls_ylim=[0.85, 1.15],
    #                                density=True,
    #                                tag=tag+n+'_pt_all',
    #                                legLoc=[0.7, 0.3, 0.9, 0.55],
    #                                maskData = False
    #                                )
    #     cAux.SaveAs(webFolder+'/'+n+'Pt_allNorm_'+tag+'.png')
    #     outCanvas.append(cAux)
    #
    #
    # h_all = None
    # for i_q2 in range(len(binning['q2'])-1):
    #     q2_l = binning['q2'][i_q2]
    #     q2_h = binning['q2'][i_q2 + 1]
    #     name = 'mass_D0pismu_q2bin'+str(i_q2)
    #     if not name in hDic.keys(): continue
    #     print 'Creating', name
    #     hDic[name]['data'].GetXaxis().SetTitle('mass(D*#mu) [GeV]')
    #     hDic[name]['data'].GetYaxis().SetTitle('Events')
    #     cAux = plot_SingleCategory(CMS_lumi, hDic[name], scale_dic=scale_dic,
    #                                draw_pulls=True, pullsRatio=False,
    #                                addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
    #                                logy=False, legBkg=True,
    #                                min_y=1,
    #                                tag='mass_D0pismu_q2bin'+str(i_q2),
    #                                legLoc=[0.16, 0.45, 0.33, 0.8],
    #                                maskData = (not args.unblinded) and (False if i_q2 < 2 else True)
    #                                )
    #     cAux.SaveAs(webFolder+'/mass_D0pismu_q2bin'+str(i_q2)+'_'+tag+'.png')
    #     outCanvas.append(cAux)
    #     if i_q2 == 0:
    #         h_all = {}
    #         for nn in hDic[name].keys():
    #             h_all[nn] = hDic[name][nn].Clone()
    #     else:
    #         for nn in h_all.keys():
    #             h_all[nn].Add(hDic[name][nn])
    # print 'Creating mass_D0pismu_all'
    # h_all['data'].GetYaxis().SetTitle('Normalized events')
    # cAux = plot_SingleCategory(CMS_lumi, h_all, scale_dic=scale_dic,
    #                            draw_pulls=True, pullsRatio=True,
    #                            addText='Cat. '+catName,
    #                            logy=False, legBkg=True,
    #                            min_y=0,
    #                            max_y='data',
    #                            pulls_ylim=[0.85, 1.15],
    #                            density=True,
    #                            tag=tag+'mass_D0pismu_all',
    #                            legLoc=[0.2, 0.45, 0.4, 0.8],
    #                            maskData = False
    #                            )
    # cAux.SaveAs(webFolder+'/mass_D0pismu_allNorm_'+tag+'.png')
    # outCanvas.append(cAux)
    #
    # for i_q2 in range(len(binning['q2'])-1):
        q2_l = binning['q2'][i_q2]
        q2_h = binning['q2'][i_q2 + 1]
        name = 'U_miss_q2bin'+str(i_q2)
        if not name in hDic.keys(): continue
        print 'Creating', name
        hDic[name]['data'].GetXaxis().SetTitle('U_{miss} [GeV]')
        hDic[name]['data'].GetYaxis().SetTitle('Events')
        cAux = plot_SingleCategory(CMS_lumi, hDic[name], scale_dic=scale_dic,
                                   draw_pulls=True, pullsRatio=False,
                                   addText='Cat. '+catName+', {:.1f} <  q^{{2}}  < {:.1f} GeV^{{2}}'.format(q2_l, q2_h),
                                   logy=False, legBkg=True,
                                   min_y=1,
                                   tag='Umiss_q2bin'+str(i_q2),
                                   legLoc=[0.16, 0.45, 0.33, 0.8],
                                   maskData = (not args.unblinded) and (False if i_q2 < 2 else True)
                                   )
        cAux.SaveAs(webFolder+'/U_miss_q2bin'+str(i_q2)+'_'+tag+'.png')
        outCanvas.append(cAux)


    print 30*'-' + '\n\n'
    return outCanvas

def drawShapeVarPlots(card, args, tag=''):
    print '-----> Creating shape variations plots for', card

    plotsDir = webFolder + '/shapeVariations' + tag
    if not os.path.isdir(plotsDir):
        os.system('mkdir -p '+plotsDir)
        os.system('cp {d}/../index.php {d}/'.format(d=plotsDir))

    print '--> REGION:'
    for fn in glob(histo_file_dir + card + '_*.root'):
        region = os.path.basename(fn).replace(card+'_', '').replace('.root', '')
        if region.startswith('h2D'):
            continue
        if len(args.shape_var_regions) > 0:
            toBePrinted = False
            for pattern in args.shape_var_regions:
                if re.match(pattern, region):
                    toBePrinted = True
                    break
            if not toBePrinted:
                continue
        print ' ', region

        auxOut = os.path.join(plotsDir, region)
        os.system('mkdir -p '+auxOut)
        os.system('cp {d}/../index.php {d}/'.format(d=auxOut))

        tfile = rt.TFile.Open(fn, 'READ')

        keys = [k.GetName() for k in tfile.GetListOfKeys()]

        processes = [k for k in keys if not '__' in k and k != 'data_obs']
        for p in processes:
            hCentral = tfile.Get(p).Clone('hCentral')
            hCentral.SetTitle('Central')
            hCentral.GetYaxis().SetTitle('Events')
            hCentral.GetXaxis().SetTitle(region)
            hCentral.GetYaxis().SetRangeUser(0, 1.3*hCentral.GetMaximum())
            if p == 'tau':
                hCentral.Scale(SM_RDst)

            shapeVarNames = []
            for k in keys:
                if (k.startswith(p + '__') or p == 'total') and k.endswith('Up'):
                    shapeVarNames.append(k.split('__')[1][:-2])
            shapeVarNames = list(np.sort(np.unique(shapeVarNames)))

            for sVar in shapeVarNames:
                if p == 'total':
                    hUp = hCentral.Clone('hUp')
                    hUp.Reset()
                    hDown = hCentral.Clone('hDown')
                    hDown.Reset()
                    for pp in processes:
                        if pp == 'total':
                            continue
                        scale = SM_RDst if pp == 'tau' else 1.
                        if pp+'__'+sVar+'Up' in keys:
                            hUp.Add(tfile.Get(pp+'__'+sVar+'Up'), scale)
                            hDown.Add(tfile.Get(pp+'__'+sVar+'Down'), scale)
                        else:
                            hUp.Add(tfile.Get(pp), scale)
                            hDown.Add(tfile.Get(pp), scale)
                else:
                    hUp = tfile.Get(p+'__'+sVar+'Up').Clone('hUp')
                    hDown = tfile.Get(p+'__'+sVar+'Down').Clone('hDown')
                    if p == 'tau':
                        hUp.Scale(SM_RDst)
                        hDown.Scale(SM_RDst)

                hUp.SetLineColor(rt.kRed-4)
                hDown.SetLineColor(rt.kAzure+1)
                if hCentral.Integral() > 0:
                    hUp.SetTitle('+1#sigma (n:{:.0f}%)'.format(100*hUp.Integral()/hCentral.Integral()))
                    hDown.SetTitle('-1#sigma (n:{:.0f}%)'.format(100*hDown.Integral()/hCentral.Integral()))
                else:
                    hUp.SetTitle('+1#sigma (n:{:.0f})'.format(hUp.Integral()))
                    hDown.SetTitle('-1#sigma (n:{:.0f})'.format(hDown.Integral()))

                for i in range(1, hCentral.GetNbinsX()+1):
                    if hCentral.GetBinContent(i) == 0:
                        hCentral.SetBinContent(i, 1e-6)
                        hCentral.SetBinError(i, 1e-6)
                    if hUp.GetBinContent(i) == 0:
                        hUp.SetBinContent(i, 1e-6)
                    if hDown.GetBinContent(i) == 0:
                        hDown.SetBinContent(i, 1e-6)
                hl = [hCentral, hUp, hDown]

                for ih, h in enumerate(hl):
                    h.Sumw2(0)
                    h.SetMarkerStyle(2)
                    h.SetMarkerSize(2)
                    h.SetMarkerColor(h.GetLineColor())
                c = make_ratio_plot(hl,
                                    draw_opt='',
                                    leg_pos=[0.75,0.73,0.95,0.92],
                                    marginTop=0.062,
                                    ratio_bounds='auto')

                # # Drowing MC uncertianty
                h_tot = tfile.Get(p).Clone('h_tot')
                c.pad1.cd()
                h_tot.SetFillColor(rt.kGray)
                h_tot.SetLineWidth(0)
                h_tot.SetMarkerStyle(0)
                h_tot.Draw('sameE2')
                for h in hl:
                    h.Draw('same')

                g_up = rt.TGraph()
                g_up.SetPoint(0, h_tot.GetBinCenter(1)-0.5*h_tot.GetBinWidth(1), 1)
                g_down = rt.TGraph()
                g_down.SetPoint(0, h_tot.GetBinCenter(1)-0.5*h_tot.GetBinWidth(1), 1)
                for i in range(1, h_tot.GetNbinsX()+1):
                    x_low = h_tot.GetBinCenter(i) - 0.5*h_tot.GetBinWidth(i)
                    x_up = h_tot.GetBinCenter(i) + 0.5*h_tot.GetBinWidth(i)
                    c_MC = h_tot.GetBinContent(i)
                    e_MC = h_tot.GetBinError(i)
                    if c_MC > 0:
                        y_up = (c_MC+e_MC)/c_MC
                        y_down = (c_MC-e_MC)/c_MC
                    else:
                        y_up = 1.
                        y_down = 1.
                    g_up.SetPoint(2*i-1, x_low, y_up)
                    g_up.SetPoint(2*i, x_up, y_up)
                    g_down.SetPoint(2*i-1, x_low, y_down)
                    g_down.SetPoint(2*i, x_up, y_down)
                g_up.SetPoint(2*i+1, x_up, 1)
                g_down.SetPoint(2*i+1, x_up, 1)
                g_up.SetFillColorAlpha(rt.kGray, 0.5)
                g_up.SetFillStyle(1)
                g_down.SetFillColorAlpha(rt.kGray, 0.5)
                g_down.SetFillStyle(1)
                g_up.SetLineWidth(0)
                c.leg.AddEntry(g_up, 'MC stat.', 'f')
                c.statMCratio = [g_up, g_down]

                c.pad2.cd()
                # c.cd(2)
                g_up.Draw('F')
                g_down.Draw('F')
                c.ln.DrawLine(h.GetXaxis().GetXmin(), 1, h.GetXaxis().GetXmax(), 1)
                for h in c.hratio_list:
                    h.Draw('same')

                c.pad1.cd()
                txt = rt.TLatex()
                txt.SetTextSize(0.05)
                txt.SetTextAlign(13)
                txt.DrawLatexNDC(0.18, 0.91, 'Region: '+region)
                txt.DrawLatexNDC(0.18, 0.85, 'Process: '+p)
                txt.DrawLatexNDC(0.18, 0.79, 'Shape: '+sVar)

                CMS_lumi.extraText = "       Simulation Internal"
                CMS_lumi.CMS_lumi(c, -1, 0)
                c.SaveAs(auxOut + '/'+p+'__'+sVar+'.png')
        tfile.Close()
    return

def drawPrePostFitComparison(histoPre, histoPost, tag=''):
    print 20*'-', 'Pre/Post fit comaprison', tag, 20*'-'
    CMS_lumi.integrated_lumi = None
    outCanvas = []

    for p in processOrder + ['total']:
        print p
        auxOutDir = outdir+'/fig/prePostFitComparison/'+p
        if not os.path.isdir(auxOutDir):
            os.system('mkdir -p '+auxOutDir)
        auxWebDir = webFolder+'/prePostFitComparison/'+p
        if not os.path.isdir(auxWebDir):
            os.system('mkdir -p '+auxWebDir)
            os.system('cp {t}/../../index.php {t}/../ '.format(t=auxWebDir))
            os.system('cp {t}/../../index.php {t}/ '.format(t=auxWebDir))

        for c in histoPost.keys():
            if '2D' in c:
                continue
            if not p in histoPre[c].keys():
                continue
            if not p in histoPost[c].keys():
                continue
            hPre = histoPre[c][p].Clone()
            hPre.SetTitle('Prefit')
            hPre.SetLineColor(rt.kRed-4)
            normPre = hPre.Integral()
            hPre.Scale(1./hPre.Integral(), 'width')
            hPre.Sumw2(0)
            hPre.GetXaxis().SetTitle(c)
            hPre.GetYaxis().SetTitle('Normalized entries')
            hPost = histoPost[c][p].Clone()
            hPost.SetLineColor(rt.kAzure+1)
            normPost = hPost.Integral()
            if hPost.Integral() != 0:
                hPost.Scale(1./hPost.Integral(), 'width')
            hPost.SetTitle('Postfit ({:.1f}%)'.format(100*normPost/float(normPre)))
            hPost.Sumw2(0)

            for i in range(1, hPre.GetNbinsX()+1):
                if hPre.GetBinContent(i) == 0:
                    hPre.SetBinContent(i, 1e-9)
                    hPost.SetBinContent(i, 1e-9)

            can = make_ratio_plot([hPre, hPost],
                                 draw_opt='',
                                 leg_pos=[0.6,0.8,0.8,0.92],
                                 marginTop=0.062,
                                 label = c+p+tag,
                                 ratio_bounds='auto')
            CMS_lumi.CMS_lumi(can, -1, 33)
            can.SaveAs(auxOutDir+'/'+c+'_'+tag+'.png')
            can.SaveAs(auxWebDir+'/'+c+'_'+tag+'.png')

            outCanvas.append(can)

    return outCanvas

def createSingleCard(histo, category, args, fitRegionsOnly=False):
    processes = processOrder
    nProc = len(processes)

    categories = []
    for c in np.sort(histo.keys()):
        if c.startswith('h2'): continue
        if fitRegionsOnly:
            if c.startswith('ctrl_'):
                if not c[5:] in args.control_regions:
                    continue
            if args.signal_reg_proj_1d:
                aux = c.startswith(args.signal_reg_proj_1d)
            elif args.use_mva:
                aux = c.startswith('MVA')
            else:
                aux = c.startswith('Unrolled')
            aux = aux or c.startswith('ctrl_')
            if not aux:
                continue
            if (not args.unblinded) and (c.endswith('_q2bin2') or c.endswith('_q2bin3')):
                continue
            if args.no_low_q2 and (c.endswith('_q2bin0') or c.endswith('_q2bin1')):
                continue
        categories.append(c)
    nCat = len(categories)

    ######################################################
    ########## Define categories (bin) and processed
    ######################################################

    card = 'imax *\njmax *\nkmax *\n'
    card += 60*'-'+'\n'

    for k in categories:
        fname = histo_file_dir+'{}_{}.root'.format(card_name, k)
        card += 'shapes * {} {} $PROCESS $PROCESS__$SYSTEMATIC\n'.format(k, fname)
    card += 60*'-'+'\n'

    # number of events observed
    card += 'bin ' + ' '.join(categories) + '\n'
    obs = map(lambda k: '{:.0f}'.format(histo[k]['data'].Integral()), categories)
    obs = ' '.join(obs)
    card += 'observation ' + obs + '\n'
    card += 60*'-'+'\n'

    # MC expected events
    aux_bin = ''
    aux_proc_name = ''
    aux_proc_id = ''
    aux_proc_rate = ''
    for c, p in itertools.product(categories, processes):
        aux_bin += ' '+c
        aux_proc_name += ' '+p
        aux_proc_id += ' '+str(np.argmax(np.array(processes) == p))
        aux_proc_rate += ' {:.2f}'.format(histo[c][p].Integral())

    card += 'bin' + aux_bin + '\n'
    card += 'process' + aux_proc_name + '\n'
    # Zero or negative for sig and positive for bkg
    card += 'process' + aux_proc_id + '\n'
    # Expected rate
    card += 'rate' + aux_proc_rate + '\n'
    card += 60*'-'+'\n'


    ######################################################
    ########## Scale systematics uncertainties
    ######################################################
    #### pp -> bb cros-section * luminosity
    card += 'overallMcNorm'+category.trg+' rateParam * mu 1.\n'
    card += 'overallMcNorm'+category.trg+' rateParam * tau 1.\n'
    card += 'overallMcNorm'+category.trg+' rateParam * B[usd]_* 1.\n'

    # Relax control regions norm
    # card += 'ctrlNormBToDstHc'+category.trg+' rateParam ctrl_??_* B[dsu]_D* 1.\n'
    # card += 'ctrlNormBToDstPiPi'+category.trg+' rateParam ctrl_??_* B[dsu]_*PiPi 1.\n'


    #### Combinatorial background norm
    val = ' 1.50'
    aux = ''
    for n in processes:
        if n == 'dataSS_DstMu':
            aux += val
        else:
            aux += ' -'
    if val in aux:
        card += 'normDataSS'+category.trg+' lnN' + aux*nCat + '\n'

    #### Branching ratio uncertainty
    brPklLoc = '/storage/af/group/rdst_analysis/BPhysics/data/forcedDecayChannelsFactors_v2.pickle'
    with open(brPklLoc, 'rb') as f:
        decayBR = pickle.load(f)

    def brScaleSys(name, relevantProcesses=[], relUnc=0.01):
        val = ' {:.2f}'.format(1+relUnc)
        aux = ''
        for nn in processes:
            if nn in relevantProcesses:
                aux += val
            else:
                aux += ' -'
        return name + ' lnN' + aux*nCat + '\n'

    if args.free_mu_br:
        pass
        # card += 'mutauNorm rateParam * tau 1. 0.9,1.1\n'
        # card += 'mutauNorm rateParam * mu 1. 0.9,1.1\n'
    else:
        card += brScaleSys('brBd_DstMuNu', ['mu', 'tau'], relUnc=1.2/50.6)

    card += brScaleSys('DstKBr', ['Bs_MuDstK', 'Bs_TauDstK'], relUnc=1.5/5.9)

    card += brScaleSys('RDs_stst', ['Bu_TauDstPi', 'Bd_TauDstPi', 'Bd_TauDstPiPi', 'Bu_TauDstPiPi', 'Bs_TauDstK'], relUnc=0.3)

    card += brScaleSys('DuMuBr', ['Bd_DstDu', 'Bu_DstDu'], relUnc=2.5/60.8)
    card += brScaleSys('DdMuBr', ['Bd_DstDd', 'Bu_DstDd'], relUnc=2.7/158.8)
    card += brScaleSys('DsMuBr', ['Bd_DstDs', 'Bs_DstDs'], relUnc=2.1/75.4)

    # They have not been observed so we variate them alltogether like this
    card += brScaleSys('Bd_DDs1Br', ['Bd_DDs1'], relUnc=1.)
    card += brScaleSys('Bu_DDs1Br', ['Bu_DDs1'], relUnc=1.)
    #card += brScaleSys('B_DstDXXBr', ['B_DstDXX'], relUnc=1.)
    # FIXME: adding 100% uncertainty here, but I don't think this is necessary
    card += brScaleSys('B_D2stDs', ['Bu_D2stDs'], relUnc=1.)

    card += 60*'-'+'\n'

    ######################################################
    ########## Shape systematics uncertainties
    ######################################################
    mcProcStr = ''
    for p in processes:
        if 'data' in p:
            mcProcStr += ' -'
        else:
            mcProcStr += ' 1.'

    if args.beamspot_calibration:
        for ax in ['x', 'y']:
            card += category.name+'BS'+ax+' shape' + mcProcStr*nCat + '\n'

    nameSF = 'trg{}SF'.format(category.trg)
    counter = 0
    for k in histo.values()[0].keys():
        if k.startswith(processOrder[0]+'__'+nameSF + '_pt') and k.endswith('Up'):
            n = k[k.find('__')+2:-2]
            card += n + ' shape' + mcProcStr*nCat + '\n'
            counter += 1

    # card += 'muonIdSF shape' + ' 1.'*nProc*nCat + '\n'

    # Additional random tracks composition and pt
    aux = ''
    for c in categories:
        if c.startswith('ctrl_'):
            aux += mcProcStr
        else:
            aux += ' -'*nProc
    if '1.' in aux:
        auxTag = '' if args.correlate_tk_pv_frac else category.name
        card += 'randTksPV'+auxTag+' shape' + aux + '\n'
        card += 'randTksPU'+auxTag+' shape' + aux + '\n'

        cname = 'addTk_pt_cali_'+category.name
        for ccc in histo.keys():
            if ccc.startswith('ctrl_'):
                for k in histo[ccc].keys():
                    if k.startswith(processOrder[0]+'__'+cname) and k.endswith('Up'):
                        n = k[k.find('__')+2:-2]
                        card += n+' shape' + aux + '\n'
                break

    # Soft track efficiency
    for k in histo.values()[0].keys():
        if k.startswith(processOrder[0]+'__softTrkEff') and k.endswith('Up'):
            n = k[k.find('__')+2:-2]
            card += n+' shape' + mcProcStr*nCat + '\n'

    card += 'ctrl shape' + mcProcStr*nCat + '\n'

    aux_x = ''
    aux_y = ''
    aux_z = ''
    for c in categories:
        for p in processes:
            if 'data' in p:
                aux_x += ' -'
                aux_y += ' -'
                aux_z += ' -'
            else:
                if histo[c]['%s__PV_xUp' % p].Integral() <= 1e-1:
                    # FIXME: hack because when we rebin using the smeared
                    # primary vertex, occasionally some processes with a
                    # very small contribution lose the only entry in the
                    # histogram which causes combine to complain because a
                    # shape uncertainty histogram has an integral of 0.
                    aux_x += ' -'
                else:
                    aux_x += ' 1.'

                if histo[c]['%s__PV_yUp' % p].Integral() <= 1e-1:
                    aux_y += ' -'
                else:
                    aux_y += ' 1.'

                if histo[c]['%s__PV_zUp' % p].Integral() <= 1e-1:
                    aux_z += ' -'
                else:
                    aux_z += ' 1.'

    # Smearing of primary vertex position
    if not args.freeze_pv or args.b_reco in ['coll','prefit']:
        card += 'PV_x shape %s\n' % aux_x
        card += 'PV_y shape %s\n' % aux_y
        card += 'PV_z shape %s\n' % aux_z

    # B eta uncertainty
    names = []
    for k in histo.values()[0].keys():
        if k.startswith(samples_Bd[0]+'__B_eta'+category.name) and k.endswith('Up'):
            names.append(k[len(samples_Bd[0]) + 2:-2])
    for ii, n in enumerate(sorted(names)):
        card += n + ' shape' + mcProcStr*nCat + '\n'
        if ii == 2: break

    # B pT uncertainty
    if args.cal_b_pt:
        # Bd pT spectrum
        aux = ''
        for p in processes:
            if p in samples_Bd:
                aux += ' 1.'
            else:
                aux += ' -'
        names = []
        for k in histo.values()[0].keys():
            if k.startswith(samples_Bd[0]+'__BdpT'+category.name) and k.endswith('Up'):
                names.append(k[len(samples_Bd[0]) + 2:-2])
        for n in sorted(names):
            card += n + ' shape' + aux*nCat + '\n'


    # Form Factors from Hammer
    if not args.freeze_ff:
        for n_pFF in FreeParFF[args.ff_scheme]:
            aux = ''
            for p in processes:
                if p in ['tau', 'mu']:
                    # FIXME: Assume that the form factor histograms are 2 sigma
                    # away. Need to double check this.
                    aux += ' 0.5'
                else:
                    aux += ' -'
            card += 'B2Dst'+args.ff_scheme+'{} shape'.format(n_pFF) + aux*nCat + '\n'


    aux = ''
    for p in processes:
        if '_MuDstPi' in p:
            aux += ' 0.5'
        else:
            aux += ' -'
    for nEig in [1,2,3,4]:
        card += 'FF_B2DststN_BLReig{} shape'.format(nEig) + aux*nCat + '\n'
        if nEig <= 3:
            card += 'FF_B2DststW_BLReig{} shape'.format(nEig) + aux*nCat + '\n'


    if not args.skip_blop:
        aux = ''
        for p in processes:
            if '_MuDstPiPi' in p:
                aux += ' 0.5'
            else:
                aux += ' -'
        for parName in ['RhoSq', 'chi11', 'chi21', 'chi31', 'eta1']:
            card += 'FF_B2D2S_BLOP{} shape'.format(parName) + aux*nCat + '\n'


    # Dstst mix composition
    aux = ''
    for p in processes:
        if re.match('B[du]_MuDstPi\Z', p):
            aux += ' 1.'
        else: aux += ' -'

    auxList = ['brB_DstPiMuNu_'+str(id) for id, _, _, _ in uncertainties_DstPi_mix]
    auxList += ['D2420_width', 'D2430_width', 'D2460_width']
    for shapeName in auxList:
        card += shapeName+' shape' + aux*nCat + '\n'


    aux = ''
    for p in processes:
        if not re.search('MuDstPiPi\Z', p) is None:
            aux += ' 1.'
        else: aux += ' -'

    auxList = ['brB_DstPiPiMuNu_'+str(id) for id, _, _, _ in uncertainties_DstPiPi_mix]
    for nnn in auxList + ['Dst2S_width']:
        card += nnn + ' shape'  + aux*nCat + '\n'


    # Hc mix composition
    def brShapeSys(relevantSamples=[], shapeNames=[], prefix=''):
        aux = ''
        for p in processes:
            if p in relevantSamples: aux += ' 1.'
            else: aux += ' -'
        out = ''

        for n in shapeNames:
            # type = 'shapeU' if 'Kst' in n else 'shape'
            type = 'shape'
            out += prefix+n+' ' + type + aux*nCat + '\n'
        return out

    for nnn in sorted(DstHc_sample_id.keys()):
        id = DstHc_sample_id[nnn]
        shapeNames = []
        for proc_id, _, _, _ in uncertainties_DstHc_mix:
            if np.floor_divide(proc_id, 100) == id:
                shapeNames.append('br'+nnn+'_'+str(proc_id))
        card += brShapeSys([nnn], shapeNames)

    #card += brShapeSys(['B_DstDXX'], ['Bu_DstDXX_frac'])



    card += 60*'-'+'\n'

    ######################################################
    ########## MC statistical uncertainties
    ######################################################

    if not args.no_mc_stats:
        for r in args.control_regions:
            card += 'ctrl_'+r+' autoMCStats 0 1 1\n'

        if args.use_mva:
            card += 'MVA autoMCStats 2 1 1\n'
        else:
            if args.signal_reg_proj_1d:
                if not args.no_low_q2:
                    card += args.signal_reg_proj_1d+'_q2bin0 autoMCStats 0 1 1\n'
                    card += args.signal_reg_proj_1d+'_q2bin1 autoMCStats 0 1 1\n'
                if args.unblinded:
                    card += args.signal_reg_proj_1d+'_q2bin2 autoMCStats 0 1 1\n'
                    card += args.signal_reg_proj_1d+'_q2bin3 autoMCStats 0 1 1\n'
            else:
                if not args.no_low_q2:
                    card += 'Unrolled_q2bin0 autoMCStats 0 1 1\n'
                    card += 'Unrolled_q2bin1 autoMCStats 0 1 1\n'
                if args.unblinded:
                    card += 'Unrolled_q2bin2 autoMCStats 0 1 1\n'
                    card += 'Unrolled_q2bin3 autoMCStats 0 1 1\n'

        card += 60*'-'+'\n'

    ######################################################
    ########## Scorrelate systematics
    ######################################################

    signalChannel = args.signal_reg_proj_1d if args.signal_reg_proj_1d else 'Unrolled'

    # card += 'nuisance edit drop * * B2Dst'+args.ff_scheme +'.* ifexists\n'

    # Relax prior increasing width by a factor 2
    # card += 'nuisance edit add * * B2Dst'+args.ff_scheme +'.* shape 0.5 overwrite\n'

    if args.decorrelate_ff_pars:
        for n in FreeParFF[args.ff_scheme]:
            if n == 'R0':
                continue
            parName = 'B2Dst'+args.ff_scheme+n
            card += 'nuisance edit rename * ' + signalChannel+'_q2bin[01] ' + parName + ' ' + parName+'_ctrlReg'+category.name+'\n'
            card += 'nuisance edit rename * ctrl_.* ' + parName + ' ' + parName+'_ctrlReg'+category.name+'\n'
            card += 'nuisance edit rename * ' + signalChannel+'_q2bin[23] ' + parName + ' ' + parName+'_sigReg'+category.name+'\n'
            card += 'nuisance edit drop * * ' + parName +'\n'

        card += 60*'-'+'\n'

    return card

def createCombinationCard(args, fitRegionsOnly=False):
    clFull = card_location.replace('.txt', '_fitRegionsOnly.txt') if fitRegionsOnly else card_location
    cmd = 'cd '+dirname(clFull)+'; '
    cl = os.path.basename(clFull)
    cmd += 'combineCards.py'
    for c in categoriesToCombine:
        singleCatCardLoc = clFull.replace('comb', c)
        nWait = 0
        while not os.path.isfile(singleCatCardLoc):
            if nWait > 10:
                print >> sys.stderr, '[ERROR] Waited too long...goodbye.'
                sys.exit(1)
            print 'Waiting for {} card to be produced'.format(c)
            time.sleep(60)
            nWait += 1
        cmd += ' {}={}'.format(c, cl.replace('comb', c))
    cmd += ' > ' + cl
    runCommandSafe(cmd)

    # Editing the nuisace renaiming
    cardStream = open(clFull, 'r')
    lines = cardStream.readlines()
    cardStream.close()

    cardStream = open(clFull, 'w')
    # Copy the whole file w/o the nuisance edit lines
    for line in lines:
        if not line.startswith('nuisance edit'):
            cardStream.write(line)
    # Re-write them down
    if args.decorrelate_ff_pars:
        signalChannel = args.signal_reg_proj_1d if args.signal_reg_proj_1d else 'Unrolled'
        for n in FreeParFF[args.ff_scheme]:
            if n == 'R0':
                continue
            parName = 'B2Dst'+args.ff_scheme+n
            for c in categoriesToCombine:
                chName = c + '_' + signalChannel+'_q2bin[01]'
                cardStream.write('nuisance edit rename * ' + chName + ' ' + parName + ' ' + parName+'_ctrlReg'+c.capitalize()+'\n')
                chName = c + '_ctrl_.*'
                cardStream.write('nuisance edit rename * ' + chName + ' ' + parName + ' ' + parName+'_ctrlReg'+c.capitalize()+'\n')
                chName = c + '_' + signalChannel+'_q2bin[23]'
                cardStream.write('nuisance edit rename * ' + chName + ' ' + parName + ' ' + parName+'_sigReg'+c.capitalize()+'\n')
            cardStream.write('nuisance edit drop * * ' + parName +'\n')

    cardStream.close()

def createWorkspace(cardLoc):
    print '-----> Creating workspace'
    print cardLoc
    cmd = 'text2workspace.py ' + cardLoc
    cmd += ' -o ' + cardLoc.replace('.txt', '.root')
    cmd += ' --no-b-only --verbose 1 --channel-masks'
    # cmd += ' --no-wrappers'
    output = runCommandSafe(cmd)

    with open(cardLoc.replace('.txt', '_text2workspace.out'), 'w') as f:
        f.write(output)

    with open(webFolder + '/' + os.path.basename(cardLoc).replace('.txt', '_text2workspace.out'), 'w') as f:
        f.write(output)

def biasToysScan(card, out, args, seed=1, nToys=10, rVal=SM_RDst, maskStr=''):
    if not args.asimov:
        inputSpace = 'higgsCombineBestfit.MultiDimFit.mH120.root'
        if not os.path.isfile(os.path.join(out, inputSpace)):
            print '-------- Best fit snap'
            cmd = 'cd ' + out + '; '
            cmd += 'combine -M MultiDimFit'
            cmd += ' -d ' + card.replace('.txt', '.root')
            cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
            cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
            cmd += ' --setParameters r={:.2f}'.format(rVal)
            if maskStr:
                cmd += ','+maskStr
            cmd += ' --setParameterRanges r=0.1,0.5'
            cmd += ' -n Bestfit'
            cmd += ' --saveWorkspace --verbose 0'
            runCommandSafe(cmd)
        arr = rtnp.root2array(os.path.join(out, inputSpace), treename='limit')
        rVal = arr['r'][0]
        print 'Using r best fit value {:.4f}'.format(rVal)
    else:
        inputSpace = card.replace('.txt', '.root')

    print '-----> Generating toys (seed: {})'.format(seed)
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M GenerateOnly'
    cmd += ' -d ' + inputSpace
    cmd += ' --seed ' + str(seed)
    cmd += ' --noMCbonly 1'
    if args.asimov:
        cmd += ' --setParameters r={} --freezeParameters r'.format(rVal)
    else:
        cmd += ' --snapshotName MultiDimFit'
    cmd += ' --toysFrequentist -t {} --saveToys'.format(nToys)
    cmd += ' -n Toys -m {:.0f}'.format(1000*rVal)
    runCommandSafe(cmd)

    print '-----> Running the toys scans'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit'
    # cmd += ' --algo grid --points=100 -n Scan'
    cmd += ' --algo singles -n Singles'
    cmd += ' --robustFit 1 --cminDefaultMinimizerStrategy 1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --seed ' + str(seed)
    cmd += ' -d ' + card.replace('.txt', '.root')
    cmd += ' --toysFrequentist --toysFile higgsCombineToys.GenerateOnly.mH{:.0f}.{}.root -t {}'.format(1000*rVal, seed, nToys)
    cmd += ' --setParameters r={:.2f}'.format(rVal)
    if maskStr:
        cmd += ','+maskStr
    cmd += ' --setParameterRanges r=0.1,0.50'
    cmd += ' --trackParameters rgx{.*}'
    # cmd += ' --trackErrors rgx{.*}'
    cmd += ' -m {:.0f}'.format(1000*rVal)
    runCommandSafe(cmd)

def collectBiasToysResults(scansLoc, rVal=SM_RDst):
    print '-----> Collectiong bias toys scans'
    if not scansLoc[-1] == '/': scansLoc += '/'
    fnames = glob(scansLoc + 'higgsCombineScan.MultiDimFit.mH{:.0f}.*.root'.format(1000*rVal))
    res = None
    # seedsScans = []
    # for fname in fnames:
    #     idx = fname.find('.mH')
    #     seed = int(fname[idx+7:-5])
    #     seedsScans.append(str(seed))
    #     if seed > 99 and seed < 199:
    #         continue
    #     # print seed
    #     if res is None:
    #         res = getUncertaintyFromLimitTree(fname, verbose=False)
    #     else:
    #         res = np.concatenate((res, getUncertaintyFromLimitTree(fname, verbose=False)), axis=0)
    # if not res is None:
    #     r = res[:,0]
    #     rLoErr = res[:,1]
    #     rHiErr = res[:,2]
    # print 'Scan: ', ' '.join(seedsScans)

    fnames = glob(scansLoc + 'higgsCombineSingles.MultiDimFit.mH{:.0f}.*.root'.format(1000*rVal))
    seedsSingles = []
    trackedParam = None
    for fname in fnames:
        idx = fname.find('.mH')
        seed = int(fname[idx+7:-5])
        if seed > 300:
            continue
        seedsSingles.append(str(seed))

        auxRes, auxTracked = getResultsFromMultiDimFitSingles(fname, verbose=False, getTrackedParam=True)
        if res is None:
            res = auxRes
            trackedParam = auxTracked
        else:
            res = np.concatenate((res, auxRes), axis=0)
            for n in trackedParam.keys():
                trackedParam[n] += auxTracked[n]

    r = res[:,0]
    rLoErr = res[:,2]
    rHiErr = res[:,3]
    print 'Singles: ', ' '.join(seedsSingles)

    plt.rcParams.update({'font.size': 18})
    fig = plt.figure(figsize=(8,6))
    plt.errorbar(np.arange(1, 1+r.shape[0]), r, yerr=np.column_stack((rLoErr, rHiErr)).T, fmt='o', color='#1f77b4', label='Toys fit results')
    m = np.mean(r)
    sm = np.std(r)/np.sqrt(r.shape[0])
    x = [0, r.shape[0]+1]
    plt.fill_between(x, 2*[m-sm], 2*[m+sm], color='#ff7f0e', alpha=0.4)
    plt.plot(x, 2*[m], color='#d62728', lw=1, label='Toys mean')
    plt.plot(x, [rVal, rVal], 'm--', lw=2, label='Injected value')
    ymin, ymax = plt.ylim(np.min(r - 2*rLoErr), np.max(r + 2*rHiErr))
    xmin, xmax = plt.xlim()
    plt.text(xmin + 0.2*(xmax-xmin), ymin + 0.07*(ymax-ymin), 'Estimated bias: $({:.2f} \pm {:.2f}) \cdot 10^{{-2}}$ '.format(100*(m-rVal), 100*sm))
    plt.legend(loc='upper right', numpoints=1)
    plt.xlabel('Toy number')
    plt.ylabel(r'$R(D^*)$')

    webFolderBias = webFolder + '/biasStudy'
    if not os.path.isdir(webFolderBias):
        os.system('mkdir -p '+webFolderBias)
        os.system('cp {d}/../index.php {d}'.format(d=webFolderBias))
    plt.savefig(webFolderBias + '/toysResults.png')

    z = (r - rVal)/(0.5*(rLoErr + rHiErr))
    h = create_TH1D(z, name='hZtest', binning=[int(2*np.sqrt(r.shape[0])), -4, 4], axis_title=['#hat{R(D*)} - R(D*) / #sigma', 'Number of toys'])
    h.Sumw2()
    h.Fit('gaus', 'ILQ')
    rt.gStyle.SetStatY(0.95)
    c = drawOnCMSCanvas(CMS_lumi, [h])
    c.SaveAs(webFolderBias + '/zTest.png')

    if not trackedParam is None:
        for n, x in trackedParam.iteritems():
            h = create_TH1D(np.array(x), name='h'+n,
                            binning=[int(2*np.sqrt(r.shape[0])), np.min(x), np.max(x)],
                            axis_title=[n, 'Number of toys'])
            h.Sumw2()
            h.Fit('gaus', 'ILQ')
            rt.gStyle.SetStatY(0.95)
            c = drawOnCMSCanvas(CMS_lumi, [h])
            c.SaveAs(webFolderBias + '/bestFitDistribution_'+n+'.png')

def dumpNuisFromScan(tag, out):
    print 'Dumping nuisances'
    name = out+'higgsCombine{}.MultiDimFit.mH120.root'.format(tag)
    df = pd.DataFrame(rtnp.root2array(name, treename='limit'))
    aux = []
    for v in df.columns:
        if not v.startswith('trackedParam_'):
            continue
        if v == 'trackedParam_CMS_th1x':
            continue
        aux.append([v[13:], df[v].iloc[0] ])

    t = PrettyTable()
    t.field_names = ['Parameter', 'Best fit']
    for var, val in sorted(aux, key=lambda x: np.abs(x[1]), reverse=True):
        t.add_row([var, '{:.2f}'.format(val)])

    with open(webFolder+'/scanNuisanceOut_'+tag+'.txt', 'w') as dumpfile:
        dumpfile.write('{}\n'.format(t))

    # Create pulls distribution
    hNuisances = create_TH1D(np.array([x[1] for x in aux]), 'hNuisDiff', binning=[41, -4.5, 4.5],
                             axis_title=['Post-pre fit nuisance difference [#sigma]', '# Nuisance'])
    hNuisances.Sumw2()
    binWidth = hNuisances.GetBinWidth(1)
    fGaus = rt.TF1('fFit', '{}*exp(-(x-[0])*(x-[0])/(2*[1]*[1]))/({:.4f}*[1])'.format(len(aux)*binWidth, np.sqrt(2*np.pi)), -5, 5)
    fGaus.SetParameters(0,1)
    fGaus.SetParNames('#mu','#sigma')
    fGaus.SetLineColor(rt.kRed-4)
    fGaus.SetLineStyle(7)
    fGaus.SetLineWidth(3)
    hNuisances.Fit(fGaus, 'QWL')
    cAux = drawOnCMSCanvas(CMS_lumi, [hNuisances], tag='nuisDiff')
    textPave = hNuisances.FindObject('stats')
    textPave.SetY1NDC(0.7)
    textPave.SetY2NDC(0.95)
    cAux.SaveAs(webFolder+'/scanNuisanceOut_'+tag+'_distribution.png')

def runScan(tag, card, out, catName, args, rVal=SM_RDst, rLimits=[0.1, 0.7], nPoints=30, maskStr='', strategy=1, freezePars=[], draw=True, dumpNuis=False):
    if not out[-1] == '/': out += '/'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit'
    cmd += ' --algo grid --points='+str(nPoints)
    cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy='+str(strategy)
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --X-rtd MINIMIZER_analytic'
    cmd += ' -d ' + card.replace('.txt', '.root')
    if len(rLimits) == 1:
        cmd += ' --centeredRange {:.2f}'.format(rLimits[0])
    else:
        cmd += ' --rMin={:.2f} --rMax={:.2f}'.format(*rLimits)
    cmd += ' --setParameters r={:.2f}'.format(rVal)
    if maskStr:
        cmd += ','+maskStr
    if len(freezePars):
        cmd += ' --freezeParameters ' + ','.join(freezePars)
    cmd += ' --trackParameters rgx{.*}'
    cmd += ' -n ' + tag
    cmd += ' --verbose 1'
    output = runCommandSafe(cmd)
    if args.verbose:
        print output

    with open(join(webFolder,'scan_output.txt'), 'a') as f:
        f.write(output)

    if dumpNuis:
        dumpNuisFromScan(tag, out)

    if draw:
        with open(out+'renameDicLikelihoodScan.json', 'w') as f:
            json.dump({'r': 'R(D*)'}, f)

        cmd = 'cd ' + out + '; '
        cmd += '../../plot1DScan.py higgsCombine{t}.MultiDimFit.mH120.root -o scan{t}'.format(t=tag)
        cmd += ' --main-label "{} {}'.format('Obs.' if not args.asimov else 'Asimov', catName)
        if not args.unblinded: cmd += ' (blinded)'
        cmd += '"'
        cmd += ' --translate ' + out+'renameDicLikelihoodScan.json'
        runCommandSafe(cmd)
        cmd = 'cp {}scan{}.png {}/'.format(out, tag, webFolder)
        runCommandSafe(cmd)

    print 'Extracting new POI boundaries'
    res = getUncertaintyFromLimitTree(out+'higgsCombine{}.MultiDimFit.mH120.root'.format(tag))
    rLimitsOut = [res[0] - 3*res[1], res[0] + 3*res[2]]
    rValOur = res[0]
    with open(webFolder+'/scan_results.txt', 'a') as dumpFile:
        strRes = tag
        strRes += ' '*(35-len(strRes))
        strRes += '{:.3f} +{:.3f} / -{:.3f}'.format(res[0], res[2], res[1])
        if res[8] is not None:
            strRes += ' (Upper lims: {:.3f}, {:.3f})'.format(res[8], res[9])
        dumpFile.write(strRes + '\n')
    if args.show_plots:
        display(Image(filename=out+'/scan'+tag+'.png'))
    return rValOur, rLimitsOut

def categoriesCompatibility(card, out, rVal=SM_RDst, rLimits=[0.1, 0.7]):
    fLog = open(webFolder + '/categoriesCompatibility.txt', 'w')
    print '----- Running nominal fit'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo singles'
    cmd += ' -d ' + card.replace('.txt', '.root')
    cmd += ' --robustFit 1 --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --setParameters r={:.2f}'.format(rVal)
    cmd += ' --setParameterRanges r={:.4f},{:.4f}'.format(*rLimits)
    cmd += ' -n _catCompNominal'
    runCommandSafe(cmd)
    arr = rtnp.root2array(out + '/higgsCombine_catCompNominal.MultiDimFit.mH120.root', treename='limit')
    rBestFit, rBestFitDown, rBestFitUp = arr['r']
    s = 'Using r best fit value {:.4f} +/- {:.4f}'.format(rBestFit, 0.5*(rBestFitUp-rBestFitDown))
    print s
    fLog.write(s + '\n')

    print '----- Creating workspace with independent signal strength'
    wsLoc = card.replace('.txt', '_rCat.root')
    if not card.endswith('.txt'):
        print card
        raise Exception("categoriesCompatibility needs txt card input.")
    cmd = 'text2workspace.py ' + card
    cmd += ' -o ' + wsLoc
    cmd += ' -P HiggsAnalysis.CombinedLimit.PhysicsModel:multiSignalModel'
    for c in categoriesToCombine:
        cmd += ' --PO map=\'.*'+c+'.*/tau:r' + c.capitalize() + '[{},{},{}]\''.format(rVal, rLimits[0], rLimits[1])
    cmd += ' --no-b-only --verbose 1'
    output = runCommandSafe(cmd)
    text_file = open(card.replace('.txt', '_rCat_text2workspace.out'), "w")
    text_file.write(output)
    text_file.close()

    print '----- Running fit with r uncorrelated in each category'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo singles'
    cmd += ' -d ' + wsLoc
    cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -n _catComp_rCatIndep'
    runCommandSafe(cmd)

    arr = rtnp.root2array(out + '/higgsCombine_catComp_rCatIndep.MultiDimFit.mH120.root', treename='limit')
    rFit = []
    for c in categoriesToCombine:
        x = arr['r'+c.capitalize()]
        rFit.append( [x[0], 0.5*(np.max(x) - np.min(x))] )
        fLog.write(c + ': {:.4f} +/- {:.4f}'.format(*rFit[-1]) + '\n')
    rFit = np.array(rFit)
    rMean = np.sum(rFit[:,0]/np.square(rFit[:,1])) / np.sum(1./np.square(rFit[:,1]))
    print 'Average observed r: {:.4f}'.format(rMean)
    fLog.write('Average observed r: {:.4f}'.format(rMean) + '\n')

    chi2 = np.sum(np.square((rFit[:,0] - rMean)/rFit[:,1]))
    dof = rFit.shape[0] - 1
    pval = scipy_chi2.sf(chi2, dof)
    print 'Chi2 = {:.2f} ({:.1f}%)'.format(chi2, 100*pval)
    fLog.write('Chi2 = {:.2f} ({:.1f}%)'.format(chi2, 100*pval) + '\n')

    print '----- Running fit with r fixed to bestfit in each category'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo fixed'
    cmd += ' --fixedPointPOIs ' + ','.join(['r{}={:.4f}'.format(c.capitalize(), rBestFit) for c in categoriesToCombine])
    cmd += ' -d ' + wsLoc
    cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -n _catComp_rCatFixed'
    runCommandSafe(cmd)
    arr = rtnp.root2array(out + '/higgsCombine_catComp_rCatFixed.MultiDimFit.mH120.root', treename='limit')
    chi2 = 2*arr['deltaNLL'][1]
    pval = scipy_chi2.sf(chi2, dof)
    print 'Wilks Chi2 = {:.2f} ({:.1f}%)'.format(chi2, 100*pval)
    fLog.write('Wilks Chi2 = {:.2f} ({:.1f}%)'.format(chi2, 100*pval) + '\n')
    fLog.close()

    return

def defineChannelMasking(histo, args):
    visibleChannels = ['ctrl_'+r for r in args.control_regions]
    if args.use_mva:
        visibleChannels += ['MVA']
    else:
        fitVar = args.signal_reg_proj_1d if args.signal_reg_proj_1d else 'Unrolled'
        if not args.no_low_q2:
            visibleChannels += [fitVar+'_q2bin0', fitVar+'_q2bin1']
        if args.unblinded:
            visibleChannels += [fitVar+'_q2bin2', fitVar+'_q2bin3']

    if args.category == 'comb':
        visibleChannels = ['rgx{mask_[hml].*_'+r+'}' for r in visibleChannels]
    else:
        visibleChannels = ['mask_'+r for r in visibleChannels]

    channelMaskingStr = 'rgx{mask_.*}=1,'+','.join([c+'=0' for c in visibleChannels])
    return channelMaskingStr

def runFitDiagnostic(tag, card, out, forceRDst=False, maskStr='', rVal=SM_RDst, rLimits=[0.1, 0.7], seed=6741, strategy=1):
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M FitDiagnostics'
    cmd += ' --robustFit 1 --robustHesse 1 --cminDefaultMinimizerStrategy '+str(strategy)+' --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --seed ' + str(seed)
    cmd += ' -d ' + card.replace('.txt', '.root')
    if forceRDst:
        cmd += ' --skipSBFit'
        cmd += ' --setParameterRanges r={:.2f},{:.2f}'.format(0, 1)
        cmd += ' --customStartingPoint --setParameters r={:.3f}'.format(rVal)
    else:
        cmd += ' --skipBOnlyFit'
        cmd += ' --setParameterRanges r={:.2f},{:.2f}'.format(*rLimits)
        cmd += ' --setParameters r={:.3f}'.format(rVal)
    if maskStr:
        cmd += ',' + maskStr
    runName = tag + ('_RDstFixed' if forceRDst else '')
    cmd += ' -n ' + runName
    cmd += ' --saveShapes --saveWithUncertainties --saveNormalizations  --saveWorkspace'
    cmd += ' --trackParameters rgx{.*}'
    cmd += ' --plots'
    cmd += ' --verbose -1'
    output = runCommandSafe(cmd)
    if rt.gROOT.IsBatch():
        print 50*'#'
        print 20*'#' + ' Fit Diag ' + 20*'#'
        print 50*'#'
        print output
    for line in output.split('\n'):
            if 'ERROR' in line: print line.replace('ERROR', '\033[1m\x1b[31mERROR\x1b[0m')
            if 'Error' in line: print line.replace('Error', '\033[1m\x1b[31mError\x1b[0m')
            if forceRDst:
                if 'customStartingPoint' in line: print line

    if not out[-1] == '/': out += '/'
    arr = rtnp.root2array(out + 'higgsCombine{}.FitDiagnostics.mH120.{}.root'.format(runName, seed), treename='limit')
    if forceRDst:
        if len(arr['limit']) > 1:
            print '[ERROR] Multiple values for R(D*):', arr['limit']
        else:
            print 'R(D*) value fixed to', arr['limit'][0]
    else:
        try:
            c, d, u, _ = arr['limit']
            print 'R(D*) = {:.3f} +{:.3f}/-{:.3f} [{:.1f} %]'.format(c, u-c, c-d, 100*(u-d)*0.5/c)
        except:
            print '[ERROR] Limit output format not recognized'
            print arr['limit']

def getPostfitHistos(tag, out, forceRDst, histo_prefit, args):
    runName = tag + ('_RDstFixed' if forceRDst else '')

    # Get post-fit shapes
    if not out[-1] == '/': out += '/'
    n = out + 'fitDiagnostics{}.root'.format(runName)
    print 'Loading post-fit from:', n
    fFitDiagnostics = rt.TFile(n, 'READ')
    if forceRDst:
        fd = fFitDiagnostics.shapes_fit_b
    else:
        fd = fFitDiagnostics.shapes_fit_s

    histo_postfit = {}
    if args.category == 'comb':
        for catregName in [k.GetTitle() for k in fd.GetListOfKeys()]:
            c = catregName.split('_')[0]
            if not c in histo_postfit.keys():
                histo_postfit[c] = {}
            regName = catregName[len(c)+1:]
            histo_postfit[c][regName] = {}

            for n, h in histo_prefit[c][regName].iteritems():
                if '__' in n:
                    continue
                if n == 'data':
                    histo_postfit[c][regName]['data'] = h.Clone(h.GetName() + '_data')
                else:
                    h_post = h.Clone(h.GetName() + '_postfit')
                    h_fit = fd.Get(catregName+'/'+n)
                    if not h_fit:
                        print n+' missing from '+c+' '+regName
                        continue
                    for i in range(1, h_post.GetNbinsX()+1):
                        h_post.SetBinContent(i, h_fit.GetBinContent(i))
                        h_post.SetBinError(i, h_fit.GetBinError(i))

                    histo_postfit[c][regName][n] = h_post

            for k in histo_prefit[c].keys():
                if not k.startswith('h2D_q2bin'):
                    continue
                if k in histo_postfit[c].keys():
                    break
                histo_postfit[c][k] = {}
                for n in histo_prefit[c][k].keys():
                    histo_postfit[c][k][n] = histo_prefit[c][k][n].Clone()
                    histo_postfit[c][k][n].Reset()
    else:
        for regName in [k.GetTitle() for k in fd.GetListOfKeys()]:
            histo_postfit[regName] = {}

            for n, h in histo_prefit[regName].iteritems():
                if '__' in n:
                    continue
                if n == 'data':
                    histo_postfit[regName]['data'] = h.Clone(h.GetName() + '_data')
                else:
                    h_post = h.Clone(h.GetName() + '_postfit')
                    h_fit = fd.Get(regName+'/'+n)
                    if not h_fit:
                        print n+' missing from '+regName
                        continue
                    for i in range(1, h_post.GetNbinsX()+1):
                        h_post.SetBinContent(i, h_fit.GetBinContent(i))
                        h_post.SetBinError(i, h_fit.GetBinError(i))

                    histo_postfit[regName][n] = h_post

        for k in histo_prefit.keys():
            if not k.startswith('h2D_q2bin'): continue
            histo_postfit[k] = {}
            for n in histo_prefit[k].keys():
                histo_postfit[k][n] = histo_prefit[k][n].Clone()
                histo_postfit[k][n].Reset()

    h2 = fFitDiagnostics.Get('covariance_fit_' + ('b' if forceRDst else 's'))
    n = None
    nRateParamX = None
    for il, labObj in enumerate(h2.GetXaxis().GetLabels()):
        lab = labObj.GetName()
        if lab.startswith('prop_bin') and n is None:
            n = il
        elif not lab.startswith('prop_bin') and not (n is None) and nRateParamX is None:
            nRateParamX = il + 1
    if n is None:
        n = il

    nR = None
    nRateParamY = None
    for il, labObj in enumerate(reversed(h2.GetYaxis().GetLabels())):
        lab = labObj.GetName()
        if lab == 'r':
            nR = il+1
        elif lab.startswith('prop_bin'):
            nRateParamY = il
            break

    h2.Scale(100.)
    rt.gStyle.SetPaintTextFormat('.0f')
    N = h2.GetNbinsX()
    # n=80
    h2.LabelsOption("v")
    gSF = 70/float(n)

    if not nR is None:
        h2.SetMarkerSize(2.0*gSF)
        h2.GetXaxis().SetLabelSize(0.07*gSF)
        h2.GetYaxis().SetLabelSize(0.07*gSF)
        h2.GetXaxis().SetRange(1, n)
        h2.GetYaxis().SetRangeUser(nR-1, nR)
        h2.GetZaxis().SetRangeUser(-100, 100)
        h2.GetZaxis().SetNdivisions(-304)
        CC1 = drawOnCMSCanvas(CMS_lumi, [h2, h2], ['colz', 'text same'], size=(1200, 300), tag='tl1', mL=0.03, mR=0.08, mB=0.65, mT=0.1)
        CC1.SaveAs(out+'fig/correlationR'+ ('_RDstFixed' if forceRDst else '')+'.png')
        CC1.SaveAs(webFolder+'/correlationR'+ ('_RDstFixed' if forceRDst else '')+'.png')
        CC1.SaveAs(webFolder+'/correlationR'+ ('_RDstFixed' if forceRDst else '')+'.pdf')

    h2.SetMarkerSize(.5*gSF)
    h2.GetXaxis().SetLabelSize(0.02*gSF)
    h2.GetYaxis().SetLabelSize(0.02*gSF)
    h2.GetXaxis().SetRange(1, n)
    h2.GetYaxis().SetRangeUser(N-n, N)
    h2.GetZaxis().SetRangeUser(-100, 100)
    h2.GetZaxis().SetNdivisions(510)
    CC = drawOnCMSCanvas(CMS_lumi, [h2, h2], ['colz', 'text same'], size=(900, 700), tag='tl', mL=0.12, mR=0.135, mB=0.16)
    CC.SaveAs(out+'fig/covariance_zoom'+ ('_RDstFixed' if forceRDst else '')+'.png')
    CC.SaveAs(webFolder+'/covariance_zoom'+ ('_RDstFixed' if forceRDst else '')+'.png')
    CC.SaveAs(webFolder+'/covariance_zoom'+ ('_RDstFixed' if forceRDst else '')+'.pdf')

    if nRateParamY>1:
        gSF = 5/float(nRateParamY)
        h2.SetMarkerSize(1.2*gSF)
        h2.GetXaxis().SetLabelSize(0.035*gSF)
        h2.GetYaxis().SetLabelSize(0.035*gSF)
        h2.GetXaxis().SetRange(nRateParamX, N)
        h2.GetYaxis().SetRangeUser(0, nRateParamY)
        h2.GetZaxis().SetRangeUser(-100, 100)
        h2.GetZaxis().SetNdivisions(510)
        CC = drawOnCMSCanvas(CMS_lumi, [h2, h2], ['colz', 'text same'], size=(900, 700), tag='tl', mL=0.2, mR=0.135, mB=0.25)
        CC.SaveAs(out+'fig/covariance_rateParam_zoom'+ ('_RDstFixed' if forceRDst else '')+'.png')
        CC.SaveAs(webFolder+'/covariance_rateParam_zoom'+ ('_RDstFixed' if forceRDst else '')+'.png')
        CC.SaveAs(webFolder+'/covariance_rateParam_zoom'+ ('_RDstFixed' if forceRDst else '')+'.pdf')

    return histo_postfit, CC, fFitDiagnostics

def extactCovarianceBlock(tag, out, parameters=['r', 'B2DstCLNeig1'], forceRDst=False):
    print 'Foction work in progress! To be compleated!!!!'
    exit()
    return

def nuisancesDiff(tag, out, forceRDst):
    runName = tag + ('_RDstFixed' if forceRDst else '')
    cmd = 'python diffNuisances.py ' + out + '/fitDiagnostics{}.root'.format(runName)
    if forceRDst:
        cmd += ' --skipFitSB'
    else:
        cmd += ' --skipFitB'
    cmd += ' --all'
    cmd += ' --abs'
    cmd += ' -g {}/nuisance_difference'.format(out) + runName + '.root'
    output = runCommandSafe(cmd)
    print 'Done'
    nName, nValPost, nSigma = dumpDiffNuisances(output, out, tag='RDstFixed' if forceRDst else '',
                      useBonlyResults=forceRDst, parsToPrint=100)
    cmd = 'cp {}/nuisance_difference{}*txt {}/'.format(out, '_RDstFixed' if forceRDst else '', webFolder)
    runCommandSafe(cmd)

    print 'Crating nuisances difference distribution'
    hNuisances = create_TH1D(nSigma, 'hNuisDiff', binning=[41, -4.5, 4.5],
                             axis_title=['Post-pre fit nuisance difference [#sigma]', '# Nuisance'])
    hNuisances.Sumw2()
    binWidth = hNuisances.GetBinWidth(1)
    fGaus = rt.TF1('fFit', '{}*exp(-(x-[0])*(x-[0])/(2*[1]*[1]))/({:.4f}*[1])'.format(nSigma.shape[0]*binWidth, np.sqrt(2*np.pi)), -5, 5)
    fGaus.SetParameters(0,1)
    fGaus.SetParNames('#mu','#sigma')
    fGaus.SetLineColor(rt.kRed-4)
    fGaus.SetLineStyle(7)
    fGaus.SetLineWidth(3)
    hNuisances.Fit(fGaus, 'QWL')
    cAux = drawOnCMSCanvas(CMS_lumi, [hNuisances], tag='nuisDiff')
    textPave = hNuisances.FindObject('stats')
    textPave.SetY1NDC(0.7)
    textPave.SetY2NDC(0.95)
    cAux.SaveAs(webFolder+'/nuisanceDifferenceDistribution.png')

    return

def runUncertaintyBreakDownScan(card, out, catName, args, rVal=SM_RDst, rLimits=[0.1, 0.7], maskStr=''):
    if not out[-1] == '/': out += '/'
    print '--------> Running uncertainty breakdown <--------------'
    print '--------> Nominal scan'
    rValOut, rLimitsOut = runScan('Nominal', card, out, catName, args, rVal, rLimits, nPoints=150, maskStr=maskStr, strategy=args.scan_strategy, draw=False)
    sig = (rLimitsOut[1] - rValOut)/3
    rLimitsTight = [rValOut - 2*sig, rValOut + 2*sig]

    print '--------> Best fit snap'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit'
    cmd += ' --cminDefaultMinimizerStrategy=1 --robustFit 1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -d ' + card.replace('.txt', '.root')
    cmd += ' --setParameters r={:.2f}'.format(rValOut)
    if maskStr:
        cmd += ','+maskStr
    cmd += ' --setParameterRanges r={:.3f},{:.3f}'.format(*rLimitsTight)
    cmd += ' -n Bestfit'
    cmd += ' --saveWorkspace --verbose -1'
    runCommandSafe(cmd)

    print '--------> Statistical uncertanty only'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo grid --points=100'
    cmd += ' --cminDefaultMinimizerStrategy=1 --robustFit 1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -d higgsCombineBestfit.MultiDimFit.mH120.root'
    cmd += ' --snapshotName MultiDimFit'
    cmd += ' --rMin={:.3f} --rMax={:.3f}'.format(*rLimitsTight)
    cmd += ' -n StatOnly'
    cmd += ' --freezeParameters allConstrainedNuisances'
    if maskStr: cmd += ' --setParameters ' + maskStr
    cmd += ' --fastScan' # To be added if there are no free parameters otherwise
    cmd += ' --verbose -1'
    runCommandSafe(cmd)
    getUncertaintyFromLimitTree(out + 'higgsCombineStatOnly.MultiDimFit.mH120.root')

    print '--------> MC stats and Statistical uncertanty only'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo grid --points=100'
    cmd += ' --cminDefaultMinimizerStrategy=1 --robustFit 1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -d higgsCombineBestfit.MultiDimFit.mH120.root'
    cmd += ' --rMin={:.4f} --rMax={:.4f}'.format(*rLimitsTight)
    cmd += ' -n MCstat'
    cmd += ' --snapshotName MultiDimFit'
    if maskStr: cmd += ' --setParameters ' + maskStr
    cmd += ' --freezeNuisanceGroups=autoMCStats'
    cmd += ' --verbose -1'
    runCommandSafe(cmd)
    getUncertaintyFromLimitTree(out + 'higgsCombineMCstat.MultiDimFit.mH120.root')


    with open(out+'renameDicLikelihoodScan.json', 'w') as f:
        json.dump({'r': 'R(D*)'}, f)

    cmd = 'cd ' + out + '; '
    cmd += 'python ../../plot1DScan.py higgsCombineNominal.MultiDimFit.mH120.root'
    cmd += ' --main-label "{} {}'.format('Obs.' if not args.asimov else 'Asimov', catName)
    if not args.unblinded: cmd += ' (blinded)'
    cmd += '"'
    cmd += ' --others'
    cmd += ' "higgsCombineMCstat.MultiDimFit.mH120.root:Stat. + Syst.:4"'
    cmd += ' "higgsCombineStatOnly.MultiDimFit.mH120.root:Stat. only:2"'
    cmd += ' --breakdown "MC stat.,syst.,stat."'
    cmd += ' --translate ' + out+'renameDicLikelihoodScan.json'
    cmd += ' -o scanBreakdown'
    runCommandSafe(cmd)
    cmd = 'cp {}scanBreakdown.png {}/'.format(out, webFolder)
    cmd += '; cp {}scanBreakdown.pdf {}/'.format(out, webFolder)
    status, output = commands.getstatusoutput(cmd)
    runCommandSafe(cmd)
    if args.show_plots:
        display(Image(filename=out+'scanBreakdown.png'))

def runUncertaintyBreakDownTable(card, out, catName, args, rVal=SM_RDst, rLimits=[0.1, 0.7], maskStr=''):
    print '--------> Running uncertainty breakdown <--------------'
    if not out[-1] == '/':
        out += '/'
    out += 'uncertaintyBreakDownTable/'
    if not os.path.isdir(out):
        os.makedirs(out)


    uncRemaining = []
    uncAss = []
    uncNames = []

    fLog = open(webFolder + '/uncertaintyBreakDownTable_log.txt', 'w')
    print '----- Running nominal fit'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo grid --points=100'
    cmd += ' -d ' + card.replace('.txt', '.root')
    cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --setParameters r={:.2f}'.format(rVal)
    if maskStr:
        cmd += ','+maskStr
    cmd += ' --setParameterRanges r={:.4f},{:.4f}'.format(*rLimits)
    cmd += ' -n _total'
    runCommandSafe(cmd)
    res = getUncertaintyFromLimitTree(out + 'higgsCombine_total.MultiDimFit.mH120.root', verbose=False, drawPlot=False)
    r, rDown, rUp = res[0], res[5], res[6]
    uncRemaining.append(0.5*(rUp-rDown))
    uncNames.append('total')
    s = 'Nominal fit: {:.4f} +/- {:.4f}'.format(r, uncRemaining[-1])
    print s
    fLog.write(s + '\n')
    fLog.close()

    rDown = r - 2.1*(r - rDown)
    rUp = r + 2.1*(rUp - r)
    idx = cmd.find('--setParameterRanges') + len('--setParameterRanges ')
    oldRange = cmd[idx: idx + 15]
    newRange = 'r={:.4f},{:.4f}'.format(rDown, rUp)
    cmd = cmd.replace(oldRange, newRange)

    print '-------- Best fit snap'
    cmdBestFit = cmd.replace(' --algo grid --points=100', '')
    cmdBestFit = cmdBestFit.replace(' -n _total', ' -n Bestfit')
    cmdBestFit += ' --saveWorkspace --verbose 0'
    runCommandSafe(cmdBestFit)

    print '-------- Breaking the uncertainty'
    def extractUncertainty(tag, type='scan'):
        if type=='fit':
            arr = rtnp.root2array(out + 'higgsCombine_'+tag+'.MultiDimFit.mH120.root', treename='limit')
            r, rDown, rUp = arr['r']
        elif type=='scan':
            res = getUncertaintyFromLimitTree(out + 'higgsCombine_'+tag+'.MultiDimFit.mH120.root', verbose=False, drawPlot=False)
            r, rDown, rUp = res[0], res[5], res[6]
        else:
            raise Exception("Type not recognised")
        uncRemaining.append(0.5*(rUp-rDown))
        uncNames.append(tag)
        uncAss.append(np.sqrt(uncRemaining[-2]**2 - uncRemaining[-1]**2))
        s = tag + ': {:.4f} +/- {:.4f} ({:.4f})'.format(r, uncRemaining[-1], uncAss[-1])
        print s
        fLog = open(webFolder + '/uncertaintyBreakDownTable_log.txt', 'a')
        fLog.write(s + '\n')
        fLog.close()
        return 0.5*(rUp-rDown), [r - 2.1*(r - rDown), r + 2.1*(rUp - r)]

    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo grid --points=100'
    cmd += ' -d higgsCombineBestfit.MultiDimFit.mH120.root'
    cmd += ' --robustFit 1'
    cmd += ' --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    if maskStr:
        cmd += ' --setParameters '+maskStr
    cmd += ' --setParameterRanges ' + newRange
    cmd += ' --snapshotName MultiDimFit'
    cmd += ' --freezeNuisanceGroups=autoMCStats'

    # MC statistics uncertanty
    print '----> Freezing MC stat'
    cmdAux = cmd + ' -n _MCstat'
    runCommandSafe(cmdAux)
    dr, rLims = extractUncertainty('MCstat')
    idx = cmd.find('--setParameterRanges') + len('--setParameterRanges ')
    cmd = cmd.replace(cmd[idx: idx + 15], 'r={:.4f},{:.4f}'.format(*rLims))
    print ' '

    groupsDefFile = os.path.join(basedir, 'Combine/uncertaintyBreakdownTableGroups.yml')
    groups = load_yaml(groupsDefFile)
    frozenNuisance = []
    firstToRun = ''
    for ig, group in enumerate(groups):
        if args.freeze_ff and group['tag'] == 'formFactors':
            continue
        print '----> Freezing ' + group['tag']
        frozenNuisance += group['nuisance']
        cmdAux = cmd + ' -n _' + group['tag']
        cmdAux += ' --freezeParameters ' + ','.join(frozenNuisance)
        if not firstToRun or firstToRun == group['tag']:
            runCommandSafe(cmdAux)
            firstToRun = ''
        dr, rLims = extractUncertainty(group['tag'])
        idx = cmd.find('--setParameterRanges') + len('--setParameterRanges ')
        cmd = cmd.replace(cmd[idx: idx + 15], 'r={:.4f},{:.4f}'.format(*rLims))

        print ' '

    # Remove all of them (statistical)
    print '----> Freezing all nuisances'
    cmd = cmd.replace(' --freezeNuisanceGroups=autoMCStats', ' --freezeParameters allConstrainedNuisances')
    # cmd = cmd.replace(' --freezeNuisanceGroups=autoMCStats', ' --freezeParameters rgx{.*}')
    cmd += ' -n _remainingNuis'
    cmd += ' --fastScan'
    runCommandSafe(cmd)
    uncStat, _ = extractUncertainty('remainingNuis', type='scan')
    uncNames.append('stat')
    uncAss.append(uncStat)
    s = 'Statistics: XX +/- {:.4f} ({:.4f})'.format(uncStat, uncStat)
    print s
    fLog = open(webFolder + '/uncertaintyBreakDownTable_log.txt', 'a')
    fLog.write(s + '\n')

    dicDump = {'uncStat': uncStat, 'uncNames': uncNames, 'uncAss': uncAss}
    pickle.dump(dicDump, open(out+'/results.pkl', 'wb'))

    totExt = np.sqrt(np.sum(np.square(uncAss)))
    s = '\n\nUncertianty sum check: {:.4f}, Delta = {:1.2e}'.format(totExt, totExt - uncRemaining[0])
    print s
    fLog.write(s + '\n')
    fLog.close()

    print '\n\n-----------> Creating latex table'
    supplementDic = {'MCstat'       : 'Finite MC sample size',
                     'stat'         : 'Statistical',
                     'remainingNuis': 'Others'
                    }
    fTable = open(webFolder + '/uncertaintyBreakDownTable_latex.txt', 'w')
    s = r'\begin{tabular}{|lr|}' + '\n'
    s += r' \hline' + '\n'
    s += r' Uncertianty Source & Size [$10^{-2}$] \\' + '\n'
    s += r' \hline' + '\n'
    s += r' \hline' + '\n'

    uncSys = np.sqrt(uncRemaining[0]**2 - uncRemaining[-1]**2)
    s += r' Systematics & ' + '{:.2f}'.format(100*uncSys) + r' \\' + '\n'
    s += r' \hline' + '\n'
    fTable.write(s)

    for i in range(1, len(uncNames)):
        n = uncNames[i]
        s = ' '
        if n in supplementDic.keys():
            title = supplementDic[n]
        else:
            title = groups[i-2]['title']

        if n in ['stat']:
            fTable.write(r' \hline' + '\n')
        else:
            title = '\hspace{3mm} ' + title

        s += title + ' & '
        if uncAss[i-1] > 0.0001:
            s += '{:.2f}'.format(100*uncAss[i-1])
        else:
            s += '< 0.01'
        s += r' \\' + '\n'
        fTable.write(s)

    s = r' \hline' + '\n'
    s += r' Total & ' + '{:.2f}'.format(100*uncRemaining[0]) + r' \\' + '\n'
    s += r' \hline' + '\n'
    s += r'\end{tabular}' + '\n'
    fTable.write(s)
    fTable.close()

    return

def externalizeUncertainty(card, out, parameters=['B2DstCLNeig1', 'B2DstCLNeig2', 'B2DstCLNeig3'], center='preFit', sigma=1, tag='FF', rVal=SM_RDst, rLimits=[0.1, 0.7]):
    tag += '_center' + center

    if not out[-1] == '/':
        out += '/'
    out += 'externalization_' + tag
    if not os.path.isdir(out):
        os.makedirs(out)

    fLog = open(webFolder + '/externalize_'+tag+'.txt', 'w')
    s = 'Externalizing paramters:' + ' '.join(parameters) + '\nCenter: ' +  center
    print s
    fLog.write(s + '\n')

    if center == 'postFit':
        # Creating bestfit snapshop
        cmd = 'cd ' + out + '; '
        cmd += 'combine -M MultiDimFit'
        cmd += ' -d ' + card.replace('.txt', '.root')
        cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
        cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
        cmd += ' --setParameters r={:.2f}'.format(rVal)
        cmd += ' --setParameterRanges r={:.4f},{:.4f}'.format(*rLimits)
        cmd += ' -n Bestfit'
        cmd += ' --saveWorkspace --verbose 0'
        cmd += ' --trackParameters ' + ','.join(parameters)
        runCommandSafe(cmd)

        inputSpace = 'higgsCombineBestfit.MultiDimFit.mH120.root'
        arr = rtnp.root2array(out + '/higgsCombineBestfit.MultiDimFit.mH120.root', treename='limit')
        centralValue = {}
        for n in parameters:
            centralValue[n] = arr['trackedParam_'+n][0]
    elif center == 'preFit':
        inputSpace = card.replace('.txt', '.root')
        centralValue = dict.fromkeys(parameters, 0)
    else:
        print 'Center option', center, 'not recognized'

    for n in parameters:
        s = n + ' central value: {:.2f}'.format(centralValue[n])
        print s
        fLog.write(s + '\n')

    print '----- Running central fit'
    cmd = 'cd ' + out + '; '
    cmd += 'combine -M MultiDimFit --algo singles'
    cmd += ' -d ' + inputSpace
    cmd += ' --robustFit 1  --cminDefaultMinimizerStrategy=1 --X-rtd MINIMIZER_analytic'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' --rMin={:.3f} --rMax={:.3f}'.format(*rLimits)
    cmd += ' --setParameters r={:.3f},'.format(rVal) + ','.join([n+'={:.2f}'.format(centralValue[n]) for n in parameters])
    cmd += ' --freezeParameters ' + ','.join(parameters)
    cmd += ' -n _ext'+tag+'_central'
    runCommandSafe(cmd)
    arr = rtnp.root2array(out + '/higgsCombine_ext'+tag+'_central.MultiDimFit.mH120.root', treename='limit')
    rCentral, d, u = arr['r']
    drCentral = (u-d)*0.5
    s = 'Central value {:.4f} +/- {:.4f}\n'.format(rCentral, drCentral)
    print s
    fLog.write(s + '\n')

    deltaR = np.zeros((len(parameters), 2))
    for ip, p in enumerate(parameters):
        rMod = []
        for mod in [sigma, -sigma]:
            newValueStr = p+'={:.2f}'.format(centralValue[p] + mod)
            cmdAux = cmd.replace(p+'={:.2f}'.format(centralValue[p]), newValueStr)
            cmdAux = cmdAux.replace(tag+'_central', tag+'_variation')
            runCommandSafe(cmdAux)
            arr = rtnp.root2array(out + '/higgsCombine_ext'+tag+'_variation.MultiDimFit.mH120.root', treename='limit')
            r, d, u = arr['r']
            dr = (u-d)*0.5
            s = '{} {:+.1f} ({:+.1f}): {:.4f} +/- {:.4f}'.format(p, mod, centralValue[p] + mod, r, dr)
            print s
            fLog.write(s + '\n')
            rMod.append(r)
        rMod = np.sort(rMod)
        s = p+' variation: {:.4f} {:+.4f}/{:+.4f}\n'.format(rCentral, rMod[1]-rCentral, rMod[0]-rCentral)
        print s
        fLog.write(s + '\n')
        deltaR[ip] = rMod

    extUnc = np.sqrt(np.sum(np.square(0.5*(deltaR[:,1] - deltaR[:,0]))))
    totUnc = np.hypot(drCentral, extUnc)

    s = 'Externalized result: {:.4f} +/- {:+.4f} (prof.) +/- {:+.4f} (ext.) = {:.4f} +/- {:+.4f}'.format(rCentral, drCentral, extUnc, rCentral, totUnc)
    print s
    fLog.write(s + '\n')
    fLog.close()
    return

def runNuisanceImpacts(card, out, catName, maskStr='', rVal=SM_RDst, submit=True, collect=True):
    if not out[-1] == '/': out += '/'

    if submit:
        if os.path.isdir(out+'impactPlots'):
            os.system('rm -rf '+out+'impactPlots')
        os.mkdir(out+'impactPlots')

        print '----- Running initial fit'
        cmd = 'cd {}impactPlots; '.format(out)
        cmd += ' combineTool.py -M Impacts --doInitialFit -m 0'
        cmd += ' --robustFit 1 --X-rtd MINIMIZER_analytic'
        cmd += ' --cminDefaultMinimizerStrategy=1'
        cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
        cmd += ' -d ' + card.replace('.txt', '.root')
        cmd += ' --setParameters r={:.2f}'.format(rVal)
        if maskStr:
            cmd += ','+maskStr
        cmd += ' --setParameterRanges r=0,0.6'
        cmd += ' --verbose 1'
        runCommandSafe(cmd)

        # If running on Tier2 condor remmeber to add this line to CombineToolBase.py ln 11
        # ``source /cvmfs/cms.cern.ch/cmsset_default.sh``
        print '----- Running all the fits'
        cmd = 'cd {}impactPlots;'.format(out)
        cmd += ' combineTool.py -M Impacts --doFits -m 0'
        cmd += ' --robustFit 1 --X-rtd MINIMIZER_analytic'
        cmd += ' --cminDefaultMinimizerStrategy=1'
        cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
        cmd += ' --parallel 100 --job-mode condor --task-name combineImpacts_'+os.path.basename(card).replace('.txt', '')
        cmd += ' --sub-opts "{}"'.format(stringJubCustomizationCaltechT2.replace('"', '\\\"').replace('$', '\$'))
        cmd += ' -d ' + card.replace('.txt', '.root')
        cmd += ' --setParameters r={:.2f}'.format(rVal)
        if maskStr:
            cmd += ','+maskStr
        cmd += ' --verbose -1'
        runCommandSafe(cmd)

    if collect:
        status, output = commands.getstatusoutput('condor_q')
        while 'combineImpacts_'+os.path.basename(card).replace('.txt', '') in output:
            time.sleep(30)
            status, output = commands.getstatusoutput('condor_q')
            for l in output.split('\n'):
                if 'combineImpacts_'+os.path.basename(card).replace('.txt', '') in l:
                    print l
                    sys.stdout.flush()
        cmd = 'cd {}impactPlots;'.format(out)
        cmd += ' combineTool.py -M Impacts -o impacts.json -m 0'
        cmd += ' -d ' + card.replace('.txt', '.root')
        runCommandSafe(cmd)


        rename = {
        'r': 'R(D*)',
        'mutauNorm': 'Norm B#rightarrow D*l#nu',
        'BdpT': 'B^{0} p_{T} spectrum',
        'BuPt': 'B^{+} p_{T} spectrum',
        'B2DstCLNR0':'R_{0} (CLN B#rightarrow D*l#nu)',
        'B2DstCLNeig1':'#lambda_{1} (CLN B#rightarrow D*l#nu)',
        'B2DstCLNeig2':'#lambda_{2} (CLN B#rightarrow D*l#nu)',
        'B2DstCLNeig3':'#lambda_{3} (CLN B#rightarrow D*l#nu)',
        'trgSF': 'Trigger scale factor',
        'trkEff': 'Tracking efficiency (control to signal region transfer factor)',
        'B2DstHcTransferFactor': 'Control to signal region transfer factor due to charged D decays',
        'softTrkEff': 'Soft tracks tracking efficiency',
        'softTrkEff_w': 'Soft tracks tracking efficiency (w)',
        'softTrkEff_s': 'Soft tracks tracking efficiency (s)',
        'tkPVfrac': 'Additional tracks origin',
        'overallNorm': 'Overall norm',
        'overallNormMu7_IP4': 'Overall norm (Low)',
        'overallNormMu9_IP6': 'Overall norm (Mid)',
        'overallNormMu12_IP6': 'Overall norm (High)',
        'fDststWide': 'D^{**} wide resonances fraction',
        'D2420_width': 'Width D**(2420)',
        'D2430_width': 'Width D**(2430)',
        'D2460_width': 'Width D**(2460)',
        'RDs_stst': 'R(D**_{(s)})',
        'Dst2S_width': 'Width D*(2S)',

        'brBd_DstDs' : 'Br. frac. B^{0}#rightarrow D*D_{s}',
        'brBd_DstDsst' : 'Br. frac. B^{0}#rightarrow D*D_{s}*',
        'brBd_DstDsst0' : 'Br. frac. B^{0}#rightarrow D*D_{s0}*',
        }
        for c in ['High', 'Mid', 'Low']:
            rename['tkPVfrac'+c] = 'Additional tracks origin ({})'.format(c)
            for i in range(1,5):
                s = str(i)
                rename['BdpT'+c+'_lam'+s] = 'B^{0} p_{T} '+c+' #lambda_{'+s+'}'
                rename['BuPt'+c+'_lam'+s] = 'B^{+} p_{T} '+c+' #lambda_{'+s+'}'

        procName_dic = {
        'mu'        : 'B^{0}#rightarrow D*#mu#nu',
        'tau'       : 'B^{0}#rightarrow D*#tau#nu',
        'DstPi'     : 'B#rightarrow D**(D*#pi)l#nu',
        'DstPiPi'   : 'B#rightarrow D**(D*#pi#pi)l#nu',
        'DstK'      : 'B_{s}#rightarrow D**_{s}(D*K)l#nu',
        'DuMu'      : 'D^{0}#rightarrow #muX',
        'DdMu'      : 'D^{+}#rightarrow #muX',
        'DsMu'      : 'D_{s}#rightarrow #muX',
        }
        for n in procName_dic: rename[n+'Br'] = 'Br. frac. ' + procName_dic[n]

        with open(out+'impactPlots/impacts.json', 'r') as f:
            d = json.load(f)
        for par in d['params']:
            name = str(par['name'])
            if name.startswith('prop_bin'):
                label = name.replace('prop_bin', 'MC stat. ')
                label = label.replace('M2_miss_', 'M^{2}_{miss} ')
                label = label.replace('Est_mu_', 'E*_{#mu} ')
                label = label.replace('q2bin', '[b_{q^{2}}=')
                label = label.replace('_bin', '] ')
                rename[name] = label + 10*' '
            elif re.match(r'trgMu[0-9]+_IP[0-9]+SF_pt[0-9]+', name):
                label = 'Trigger scale factors ' + re.search(r'Mu[0-9]+_IP[0-9]', name).group(0)
                idx = name.find('_pt')
                label += ' bin ' + name[idx+3:]
                rename[name] = label
        with open(out+'impactPlots/rename.json', 'w') as f:
            json.dump(rename, f)

        cmd = 'cd {};'.format(out)
        cmd += 'plotImpacts.py -i impactPlots/impacts.json -o impacts -t impactPlots/rename.json --max-pages 1'
        runCommandSafe(cmd)
        cmd = 'cp {}impacts.pdf {}/'.format(out, webFolder)
        runCommandSafe(cmd)

        cmd = 'cd {};'.format(out)
        cmd += 'plotImpacts.py -i impactPlots/impacts.json -o impacts_full -t impactPlots/rename.json'
        runCommandSafe(cmd)
        cmd = 'cp {}impacts_full.pdf {}/'.format(out, webFolder)
        runCommandSafe(cmd)

def runCommand(input_args):
    i, cmd = input_args
    print 'Start', i
    st = time.time()
    status, output = commands.getstatusoutput(cmd)
    print 'Done {} in {:.1f} min'.format(i, (time.time() - st)/60.0 )
    return [status, output]

def runGoodnessOfFit(tag, card, out, algo, args, maskEvalGoF='', fixRDst=False, rVal=SM_RDst):
    # Always to be tun with fitRegionsOnly cards
    tag += '_algo'+algo
    if fixRDst:
        tag += '_fixRDst'

    if not out[-1] == '/': out += '/'
    gofOutdir = out + 'goodnessOfFit'+tag
    if os.path.isdir(gofOutdir): os.system('rm -rf ' + gofOutdir)
    os.system('mkdir ' + gofOutdir)


    cmd = 'cd ' + gofOutdir + '; '
    cmd += 'combine -M GoodnessOfFit'
    cmd += ' --algo=saturated  --toysFrequentist' if algo=='Sat' else ' --algo='+algo
    cmd += ' --X-rtd MINIMIZER_analytic  --cminDefaultMinimizerStrategy=1'
    cmd += ' --cminFallbackAlgo Minuit2,Migrad,0'
    cmd += ' -d ' + card.replace('.txt', '.root')
    if fixRDst:
        cmd += ' --freezeParameters r --setParameters r={:.3f}'.format(rVal)
    if maskEvalGoF:
        cmd += ' --setParametersForEval ' + maskEvalGoF
    cmd += ' -n Obs'+tag
    cmd += ' -t 0 -s 100'
    cmd += ' --verbose -1'
    runCommandSafe(cmd)
    arr = rtnp.root2array(gofOutdir+'/higgsCombineObs'+tag+'.GoodnessOfFit.mH120.100.root', treename='limit')
    s_obs = arr['limit'][0]
    print 'Observed test statistics: {:.1f}'.format(s_obs)


    # Run the test stat toy distribution
    cmdToys = cmd.replace('-n Obs', '-n Toys')
    nToysPerRep = 40 if args.run_in_job else 5
    cmdToys = cmdToys.replace('-t 0 -s 100', '-t {} -s -1'.format(nToysPerRep))
    print cmdToys

    nRep = 5 if args.run_in_job else 20
    p = Pool(min(20,nRep))
    outputs = p.map(runCommand, [[i, cmdToys] for i in range(nRep)])
    for s,o in outputs:
        if s: print o


    # Get the p-value
    s_toys = []
    for name_toys in glob(gofOutdir+'/higgsCombineToys'+tag+'.GoodnessOfFit.*.root'):
        arr = rtnp.root2array(name_toys, treename='limit')
        s_toys += list(arr['limit'])
    p_val = np.sum(s_toys > s_obs)/float(len(s_toys))

    plt.rcParams.update({'font.size': 18})
    fig = plt.figure(figsize=(8,6))
    content, center, _ = plt.hist(s_toys, weights=np.ones_like(s_toys)/float(len(s_toys)),
                                  alpha=0.7, label='Toys ({:.0f})'.format(float(len(s_toys))))
    nDigits = '3' if s_obs < 1 else '1'
    plt.plot([s_obs, s_obs], [0, np.max(content)], 'm--', label=('Obs: {:.'+nDigits+'f}\np-val {:.1f}%').format(s_obs, 100*p_val))
    plt.legend(loc='upper right')
    plt.xlabel('Test statistic (algo {})'.format(algo))
    plt.ylabel('Probability / {:.1f}'.format(0.5*(center[2]-center[1])))
    text = 'Cat. '+args.category.capitalize()
    if fixRDst:
        text += ' , R(D*) fixed'
    plt.text(0.04, 0.93, text, transform = plt.gca().transAxes)
    plt.savefig(webFolder + '/resultsGoF'+tag+'.png')

    strRes = tag
    strRes += ' '*(45-len(strRes))
    strRes += '{:.3f} ({:.1f}%)'.format(s_obs, 100*p_val)
    strRes += ' '*(70-len(strRes))
    strRes += '{:.3f} {:.3f} {:.3f}'.format(np.percentile(s_toys, 50), np.percentile(s_toys, 95), np.percentile(s_toys, 99))
    print strRes
    with open(webFolder+'/GoF_results.txt', 'a') as dumpFile:
        dumpFile.write(strRes + '\n')
    os.system('echo "{}" >> {}GoF_results.txt'.format(strRes, out));

def submitRunToCondor(args):
    jobDir = outdir + '/condor'
    if not os.path.exists(jobDir):
        os.makedirs(jobDir)

    jN = 0
    for path in glob('%s/job_*.jdl' % jobDir):
        filename = os.path.basename(path)
        root, ext = os.path.splitext(filename)
        number = int(root.split("_")[1])
        jN = max(jN, number+1)

    arguments = os.environ['HOME'] + ' '
    arguments += os.environ['PWD'] + ' '
    arguments += ' '.join(sys.argv).replace(' --submit', ' --run-in-job')

    environment = '"BPH_RD_ANALYSIS=%s"' % os.path.join(os.environ['HOME'],'RDstAnalysis/BPH_RD_Analysis')
    mem = 20000
    cpus = np.ceil(float(mem)/4000)

    if 'fitDiag' in args.step or args.category == 'comb':
        jobqueue = '"Normal"'
    else:
        jobqueue = '"Short"'

    job = CONDOR_TEMPLATE.format(basedir=basedir, environment=environment, arguments=arguments, outdir=jobDir, jobqueue=jobqueue, mem=mem, cpus=cpus, jN=jN)

    with open('%s/job_%i.jdl' % (jobDir,jN), 'w') as fsub:
        fsub.write(job)

    cmd = 'cd %s; condor_submit job_%i.jdl' % (jobDir,jN)
    cmd += ' -batch-name %s_%i' % (card_name,jN)
    runCommandSafe(cmd)
    print 'Jobs submitted'

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Script used to run combine on the R(D*) analysis.', epilog='Example: ./runCombine.py -c low', add_help=True)
    parser.add_argument('-v', '--card-tag', default='test_', help='Card name initial tag.')
    parser.add_argument('-c', '--category', type=str, default='high', choices=['single', 'low', 'mid', 'high', 'comb'], help='Category.')
    parser.add_argument('-s', '--skim-tag', default='', type=str, help='Tag to append to the skimmed directory.')
    parser.add_argument('--skim-tag-rd', default=None, type=str, help='Tag to append to the skimmed directory for data.')
    parser.add_argument('--bare-mc', action='store_true', help='Use bare MC instead of the corrected one.')
    parser.add_argument('--max-events', default=None, type=int, help='Max number of MC events to load per sample.')
    parser.add_argument('--cal-b-pt', default=True, action='store_true', help='include B pt corrections')
    parser.add_argument('--ff-scheme', default='CLN', choices=['CLN', 'BLPR', 'NoFF'], help='Form factor scheme to use.')
    parser.add_argument('--lumi-mult', default=1., type=float, help='Luminosity multiplier for asimov dataset. Only works when running with --asimov.')
    parser.add_argument('--beamspot-calibration', action='store_true', help='Apply beam spot calibration.')

    parser.add_argument('--use-mva', action='store_true', help='Use MVA in the fit.')
    parser.add_argument('--b-reco', default='vtx', type=str, choices=['vtx', 'coll', 'prefit'], help='Select reconstruction mode for B.')
    parser.add_argument('--signal-reg-proj-1d', default='', choices=['M2_miss', 'Est_mu', 'U_miss'], help='Use 1D projections in signal region instead of the unrolled histograms')
    parser.add_argument('--unblinded', action='store_true', help='Unblind the fit regions.')
    parser.add_argument('--no-low-q2', action='store_true', help='Mask the low q2 signal regions.')
    parser.add_argument('--control-regions', default=['p__mHad', 'm__mHad', 'pp_mHad', 'mm_mHad', 'pm_M2miss', 'pm_q2'], help='Control regions to use', nargs='*')
    parser.add_argument('--cut-mu-ps', action='store_true', help='Restrict phase space. See data loading for more info.')

    parser.add_argument('--correlate-tk-pv-frac', default=False, action='store_true', help='Correlate tkPVfrac in all categories.')
    parser.add_argument('--freeze-ff', action='store_true', help='Freeze form factors to central value.')
    parser.add_argument('--free-mu-br', default=True, help='Make muon branching fraction with a rate parameter (flat prior).')
    parser.add_argument('--asimov', action='store_true', help='Use Asimov dataset insted of real data.')
    parser.add_argument('--no-mc-stats', action='store_true', help='Do not include MC stat systematic.')

    parser.add_argument('--dump-weights-tree', action='store_true', help='Dump tree with weights for skimmed events.')

    availableSteps = ['clean', 'histos', 'preFitPlots', 'shapeVarPlots',
                      'card', 'workspace',
                      'bias', 'scan', 'catComp',
                      'fitDiag', 'postFitPlots',
                      'uncBreakdownScan', 'uncBreakdownTable',
                      'externalize',
                      'impacts', 'GoF']
    defaultPipelineSingle = ['histos', 'preFitPlots', 'card', 'workspace', 'scan', 'GoF', 'fitDiag', 'postFitPlots']
    defaultPipelineComb = ['preFitPlots', 'card', 'workspace', 'scan', 'catComp', 'uncBreakdownTable', 'GoF', 'fitDiag', 'postFitPlots', 'uncBreakdownScan']
    # histos preFitPlots shapeVarPlots card workspace scan fitDiag postFitPlots uncBreakdownScan GoF
    parser.add_argument('--step', type=str, default=[], choices=availableSteps, help='Analysis steps to run.', nargs='+')
    parser.add_argument('--submit', action='store_true', help='Submit a job instead of running the call interactively.')
    parser.add_argument('--run-in-job', action='store_true', help='Not for user, reserved to jobs calls.')

    parser.add_argument('--validate-card', action='store_true', help='Run combine card validation.')
    parser.add_argument('--decorrelate-ff-pars', action='store_true', help='Decorrelte form factors parameters')

    parser.add_argument('--force-rdst', action='store_true', help='Perform fit fixing R(D*) to 0.295')
    parser.add_argument('--seed', default=6741, type=int, help='Seed used by Combine')
    parser.add_argument('--rdst-lims', default=[], type=float, help='Initial boundaries for R(D*).', nargs='+')

    # Shape variations options
    parser.add_argument('--shape-var-regions', default=[], type=str, help='Regions (or regular expression) to be plotted in shape variations.', nargs='*')

    # Bias options
    parser.add_argument('--run-bias-toys', default=False, action='store_true', help='Only generate toys and run scans for bias, do not collect results.')
    parser.add_argument('--ntoys', default=10, type=int, help='Number of toys to run')
    parser.add_argument('--toys-rdst', default=0.295, type=float, help='R(D*) value used to generate the toys.')
    parser.add_argument('--run-bias-analysis', action='store_true', help='Only analyze bias scans which have been previously produced.')

    # Scan options
    parser.add_argument('--scan-strategy', default=2, type=int, help='Minimizer strategy for the scan.')
    parser.add_argument('--mask-scan', type=str, default=[], nargs='+', help='Channels to mask during likelyhood scan. If this list is non empty, the full card is used (default is fitregionsOnly).')
    parser.add_argument('--scan-tag', type=str, default='')
    parser.add_argument('--freeze-pars-scan', type=str, default=[], nargs='+')

    # Externalization options
    parser.add_argument('--extern-pars', default=['B2DstCLNeig1', 'B2DstCLNeig2', 'B2DstCLNeig3'], type=str, help='Parameters to externalize.', nargs='+')
    parser.add_argument('--extern-sigma', default=1., type=float, help='Externalization sigmas.')
    parser.add_argument('--extern-tag', default='FF', type=str, help='Externalization tag.')
    parser.add_argument('--extern-center', default='postFit', type=str, choices=['preFit', 'postFit'], help='Externalization tag.')

    # Impacts options
    parser.add_argument('--collect-impacts', action='store_true', help='Only collect impact fits which have been previously run')
    parser.add_argument('--sub-only-impacts', action='store_true', help='Only submit impact fits, do not collect results')

    # Goodness Of Fit options
    parser.add_argument('--algo-gof', type=str, default=['Sat','KS'], choices=['Sat', 'AD', 'KS'], help='Algorithm to be used for the goodness of fit test', nargs='+')
    parser.add_argument('--mask-eval-gof', type=str, default=[], nargs='+', help='Additional channels to mask during GoF evaluation')
    parser.add_argument('--tag-gof', type=str, default='all')

    parser.add_argument('--show-plots', action='store_true', help='Show plots by setting ROOT batch mode OFF (default ON)')
    parser.add_argument('--show-card', action='store_true', help='Dump card on std outoput')
    parser.add_argument('--verbose', action='count', default=1)
    parser.add_argument('--skip-blop', default=True, action='store_true', help='Skip BLOP form factor weights')
    parser.add_argument('--freeze-pv', action='store_true', help="Don't include primary vertex resolution uncertainties.")

    args = parser.parse_args()

    if args.skim_tag_rd is None:
        args.skim_tag_rd = args.skim_tag

    if not args.card_tag.endswith('_'):
        args.card_tag += '_'

    if len(args.step) == 0:
        if args.category == 'comb':
            args.step = defaultPipelineComb
        else: args.step = defaultPipelineSingle

        if args.card_tag == 'test_' and not args.submit and not args.run_in_job:
            args.step = ['clean'] + args.step

        if not args.unblinded:
            for s in args.step:
                if 'uncBreakdown' in s:
                    args.step.remove(s)

        print 'Running default steps: ' + ', '.join(args.step)

    if not args.show_plots:
        rt.gROOT.SetBatch(True)
        plt.ioff()
        plt.switch_backend('Agg')

    if len(args.rdst_lims) == 2 and args.rdst_lims[1] <= args.rdst_lims[0]:
        raise Exception("R(D*) upper limit less than lower limit!")
    elif len(args.rdst_lims) > 2:
        raise Exception("Too many R(D*) limits!")

    if args.lumi_mult != 1:
        print 'Multipling the expected luminosity by {:.1f}'.format(args.lumi_mult)
        for n in expectedLumi.keys():
            expectedLumi[n] *= args.lumi_mult
        print expectedLumi

    if args.asimov:
        CMS_lumi.extraText = "     Simulation Preliminary"

    card_name = createCardName(args)
    print 'Card name:', card_name
    print 'Control regions:', ' '.join(args.control_regions)

    outdir = join(basedir,'Combine/results/',card_name)
    if not os.path.isdir(outdir):
        os.system('mkdir -p %s/fig' % outdir)
    card_location = join(basedir,'Combine/cards/{}.txt'.format(card_name))
    histo_file_dir = join(basedir,'data/_root/histos4combine/')

    userName = basedir[basedir.find('/user/')+6:].split('/')[0]
    webFolder = '/storage/af/user/%s/public_html/BPH_RDst/Combine/%s' % (userName,card_name)
    if not os.path.isdir(webFolder):
        os.makedirs(webFolder)
        os.system('cp %s/../index.php %s' % (webFolder,webFolder))

    if args.submit:
        submitRunToCondor(args)
        exit()

    if 'clean' in args.step:
        print '-----> Cleaning previous results'
        cleanPreviousResults()
        args.step.remove('clean')

    dumpCallAndGitStatus(args)

    if 'histos' in args.step:
        if args.category == 'comb':
            print 'Histo should be created ahead running single categories'
        else:
            createHistograms(categoriesDef[args.category], args)
        args.step.remove('histos')

    if not args.step:
        exit()

    print '-----> Loading histograms'
    histo = None
    if args.category == 'comb':
        histo = {}
        for c in categoriesToCombine:
            print '---- Loading', c
            present = False
            while not present:
                if os.path.isfile(histo_file_dir+'histos_{}_ready.log'.format(card_name.replace('comb', c))):
                    n = len(glob(os.path.join(histo_file_dir, card_name.replace('comb', c)) + '_*.root'))
                    present = True
                    with open(histo_file_dir+'histos_{}_ready.log'.format(card_name.replace('comb', c)), 'r') as flog:
                        out = flog.readlines()
                    print 'Accepting', n, 'histos files from', c, 'created on', ' '.join(out).strip()
                else:
                    print 'Waiting for ' + c
                    time.sleep(60*10)
            histo[c] = loadHisto4CombineFromRoot(histo_file_dir, card_name.replace('comb', c))
    else:
        loadShapeVar = 'card' in args.step
        histo = loadHisto4CombineFromRoot(histo_file_dir, card_name, loadShapeVar=loadShapeVar, verbose=False)


    if 'preFitPlots' in args.step:
        if args.category == 'comb':
            cPre = {}
            for c in categoriesToCombine:
                cPre[c] = drawPlots('prefit'+c.capitalize(), histo[c], c.capitalize(), args, scale_dic={'tau': SM_RDst})
        else:
            cPre = drawPlots('prefit', histo, args.category.capitalize(), args, scale_dic={'tau': SM_RDst})
        args.step.remove('preFitPlots')


    if 'shapeVarPlots' in args.step:
        if args.category == 'comb':
            cPre = {}
            for c in categoriesToCombine:
                drawShapeVarPlots(card_name.replace('comb', c), args, '_'+c)
        else:
            drawShapeVarPlots(card_name, args)
        args.step.remove('shapeVarPlots')

    if 'card' in args.step:
        for fitRegionsOnly in [False, True]:
            print '-----> Creating the datacard' + (' (fit regions only)' if fitRegionsOnly else '')
            cl = card_location.replace('.txt', '_fitRegionsOnly.txt') if fitRegionsOnly else card_location
            if args.category == 'comb':
                createCombinationCard(args, fitRegionsOnly)
            else:
                card = createSingleCard(histo, categoriesDef[args.category], args, fitRegionsOnly)
                if args.show_card:
                    print 3*'\n'
                    print '--------> Dumping Combine card (fitRegionsOnly {})'.format(fitRegionsOnly)
                    print card
                    print 3*'\n'
                print 'Card location:', cl
                with open(cl, 'w') as f:
                    f.write(card)

            cmd = 'cp '+cl+' '+webFolder+'/'+os.path.basename(cl)
            runCommandSafe(cmd)


        if args.validate_card:
            cl = card_location.replace('.txt', '_fitRegionsOnly.txt')
            cmd = 'cd ' + dirname(cl) + '; '
            cmd += 'ValidateDatacards.py ' + os.path.basename(cl) + ' -p 3 -c 0.2'
            print cmd
            status, output = commands.getstatusoutput(cmd)
            if ('ERROR' in output) or ('There were  ' in output):
                print output

        print '\n'
        args.step.remove('card')

    if not args.step: exit()

    if 'workspace' in args.step:
        createWorkspace(card_location)
        createWorkspace(card_location.replace('.txt', '_fitRegionsOnly.txt'))
        print '\n'


    if 'bias' in args.step:
        biasOut = outdir + '/biasStudyToys'
        if not os.path.isdir(biasOut):
            os.makedirs(biasOut)

        both = args.run_bias_analysis and args.run_bias_toys
        if (not args.run_bias_analysis) or both:
            biasToysScan(card_location.replace('.txt', '_fitRegionsOnly.txt'), biasOut, args, args.seed, args.ntoys)
        if (not args.run_bias_toys) or both:
            collectBiasToysResults(biasOut, args.toys_rdst)

    if 'scan' in args.step:
        print '-----> Running likelyhood scan'
        if len(args.rdst_lims) > 0:
            rLimits = args.rdst_lims
        elif args.unblinded:
            rLimits = [0.1]
        elif not args.unblinded:
            if args.category == 'comb':
                rLimits = [0.3]
            else:
                rLimits = [0.3]

        if args.mask_scan:
            maskList = []
            for n in args.mask_scan:
                knList = histo.keys()
                if args.category == 'comb':
                    knList = []
                    for c, hDic in histo.iteritems():
                        for n in hDic.keys():
                            knList.append(c+'_'+n)
                for kn in knList:
                    if not re.match(n, kn) is None:
                        print 'Masking', kn
                        maskList.append('mask_'+kn+'=1')
            maskStr = ','.join(maskList)
            fit_RDst, rDst_postFitRegion = runScan(args.scan_tag, card_location, outdir,
                                                   args.category.capitalize(),
                                                   args,
                                                   maskStr=maskStr,
                                                   rLimits=rLimits, strategy=args.scan_strategy, draw=True, dumpNuis=True)
        elif args.scan_tag:
            fit_RDst, rDst_postFitRegion = runScan(args.scan_tag, card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                                                   args.category.capitalize(),
                                                   args,
                                                   freezePars=args.freeze_pars_scan,
                                                   rLimits=rLimits,
                                                   strategy=args.scan_strategy, draw=True, dumpNuis=True)
        else:
            fit_RDst, rDst_postFitRegion = runScan('Base', card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                                                   args.category.capitalize(),
                                                   args,
                                                   rLimits=rLimits,
                                                   strategy=args.scan_strategy, draw=True, dumpNuis=True)
    else:
        rDst_postFitRegion = args.rdst_lims if len(args.rdst_lims) == 2 else [0.1, 0.5]
        fit_RDst = SM_RDst
    rDst_postFitRegion[0] = max(0, rDst_postFitRegion[0])
    print '[INFO] R(D*) central value set to {:.3f}'.format(fit_RDst)
    print '[INFO] R(D*) boundaries set to [{:.3f}, {:.3f}]'.format(*rDst_postFitRegion)

    if 'catComp' in args.step:
        if not args.category == 'comb':
            print '[WARNING]: Skipping catComp because not defined for category='+args.category
        else:
            print '-----> Running categories compatibility'
            categoriesCompatibility(card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                                    rVal=SM_RDst, rLimits=rDst_postFitRegion
                                    )

    if 'GoF' in args.step:
        print '-----> Goodnees of Fit'
        maskList = []
        if args.mask_eval_gof:
            if len(args.algo_gof) > 1 or args.algo_gof[0] != 'Sat':
                print 'Only saturated algorith accept masks. Running only algo=Sat'
                args.algo_gof = ['Sat']

            for n in args.mask_eval_gof:
                for kn in histo.keys():
                    if not re.match(n, kn) is None:
                        print 'Masking', kn
                        maskList.append('mask_'+kn+'=1')
        maskStr = ','.join(maskList)
        for algo in args.algo_gof:
            runGoodnessOfFit(args.tag_gof, card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                             algo, args, fixRDst=args.force_rdst, maskEvalGoF=maskStr)
            print '-'

    if 'uncBreakdownTable' in args.step:
        runUncertaintyBreakDownTable(card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                                args.category.capitalize(),
                                scan,
                                rVal=fit_RDst, rLimits=rDst_postFitRegion)

    if 'externalize' in args.step:
        print '-----> Running externalization'
        externalizeUncertainty(card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                               parameters=args.extern_pars,
                               center=args.extern_center,
                               sigma=args.extern_sigma,
                               tag=args.extern_tag,
                               rVal=SM_RDst, rLimits=[0.1, 0.45]
                               )

    if 'fitDiag' in args.step:
        print '-----> Running fit diagnostic'
        globalChannelMaskingStr = defineChannelMasking(histo, args)
        runFitDiagnostic(args.card_tag, card_location, outdir,
                         strategy=0 if args.category == 'comb' else 1,
                         forceRDst=args.force_rdst, maskStr=globalChannelMaskingStr,
                         rVal=fit_RDst, rLimits=rDst_postFitRegion)

    if 'postFitPlots' in args.step:
        print '-----> Getting postfit results'
        histo_post, _, _ = getPostfitHistos(args.card_tag, outdir, args.force_rdst, histo, args)
        if args.category == 'comb':
            cPost = {}
            cPrePostComp = {}
            for c in categoriesToCombine:
                tag = 'postfit'+c.capitalize() + ('_RDstFixed' if args.force_rdst else '')
                cPost[c] = drawPlots(tag, histo_post[c], c.capitalize(), args)
                cPrePostComp[c] = drawPrePostFitComparison(histo[c], histo_post[c], tag=c + ('_RDstFixed' if args.force_rdst else ''))
        else:
            tag = 'postfit'+ ('_RDstFixed' if args.force_rdst else '')
            cPost = drawPlots(tag, histo_post, args.category.capitalize(), args)
            cPrePostComp = drawPrePostFitComparison(histo, histo_post, tag=('_RDstFixed' if args.force_rdst else ''))
        nuisancesDiff(args.card_tag, outdir, args.force_rdst)
        print '\n'

    if 'uncBreakdownScan' in args.step:
        runUncertaintyBreakDownScan(card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                                args.category.capitalize(),
                                args,
                                rVal=fit_RDst, rLimits=rDst_postFitRegion)

    if 'impacts' in args.step:
        print '-----> Running impact plots'
        submit, collect = True, True
        if args.collect_impacts and not args.sub_only_impacts:
            submit, collect = False, True
        elif not args.collect_impacts and args.sub_only_impacts:
            submit, collect = True, False
        runNuisanceImpacts(card_location.replace('.txt', '_fitRegionsOnly.txt'), outdir,
                           args.category.capitalize(),
                           rVal=fit_RDst, submit=submit, collect=collect)
