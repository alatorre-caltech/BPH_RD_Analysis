import numpy as np
import uproot as ur
import ROOT as rt
import root_numpy as rtnp
import pandas as pd
from glob import glob
import yaml
import os
from os.path import join, expanduser, exists, abspath
import pickle
import sys
import time

import operator
ops = {'>': operator.gt, '<': operator.lt, }

# Latest ntuple tag. This tag contains a fix for the impact parameter uncertianty on MC
NTUPLE_TAG = 'beamspot-constraint'
TRIGGER_SCALE_FACTOR = 'beamspot-constraint'
BD_CALIBRATION = 'beamspot-constraint'

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_warning(msg):
    print >> sys.stderr, bcolors.FAIL + msg + bcolors.ENDC

def load_yaml(filename):
    with open(filename,'r') as f:
        return yaml.safe_load(f)

def check_file(fn, ask=False):
    """
    Check whether a ROOT file is broken or wasn't closed properly.  If `ask` is
    True, will prompt the user for any broken file and ask the user if they
    want to delete the file or skip it. If `ask` is False, it just defaults to
    skipping.

    Returns True if the file is OK, or False if the file wasn't closed properly.
    """
    f = rt.TFile.Open(fn, 'READ')

    if not ask:
        if f is None or f.IsZombie() or f.TestBit(rt.TFile.kRecovered):
            print_warning("File '%s' wasn't closed properly. Skipping..." % fn)
            f.Close()
            return False
        if not f.GetListOfKeys().Contains("outA") or not f.Get("outA").GetListOfKeys().Contains("Tevts"):
            print_warning("File '%s' doesn't have the TTree outA/Tevts. Skipping..." % fn)
            f.Close()
            return False
        f.Close()
        return True

    if f is None or f.IsZombie() or f.TestBit(rt.TFile.kRecovered):
        answer = None
        while answer not in ('y','n','s'):
            print_warning("Delete file '%s' which wasn't closed properly? [y/n/s]" % fn)
            answer = raw_input()
        if answer == 'y':
            f.Close()
            os.remove(fn)
            return False
        elif answer == 's':
            f.Close()
            return False
    if not f.GetListOfKeys().Contains("outA") or not f.Get("outA").GetListOfKeys().Contains("Tevts"):
        answer = None
        while answer not in ('y','n','s'):
            print_warning("Delete file '%s' which doesn't have the TTree outA/Tevts? [y/n/s]" % fn)
            answer = raw_input()
        if answer == 'y':
            f.Close()
            os.remove(fn)
            return False
        elif answer == 's':
            f.Close()
            return False
    f.Close()
    return True

def drawOnCMSCanvas(CMS_lumi, dobj, opt = None, tag='', size=[800,600],
                    mL=None, mR=None, mT=None, mB=None,
                    makeLegend=False, legPos=[0.7, 0.7, 0.9, 0.9], legNames=None,
                    iPosCMS=0, drawCanvas=True):
    c = rt.TCanvas('c'+tag, 'c'+tag, 50, 50, size[0], size[1])
    c.SetTickx(0)
    c.SetTicky(0)
    if not mL is None: c.SetLeftMargin(mL)
    if not mR is None: c.SetRightMargin(mR)
    if not mT is None: c.SetTopMargin(mT)
    if not mB is None: c.SetBottomMargin(mB)

    if dobj.__class__ == rt.RooPlot:
        dobj.Draw()
    elif dobj[0].__class__ in [rt.TH1F, rt.TH1D, rt.TH2D, rt.TGraph, rt.TGraphErrors, rt.TGraphAsymmErrors, rt.TProfile, rt.TEfficiency]:
        for i, o in enumerate(dobj):
            do = ''
            if not (opt is None):
                if opt == 'same':
                    if i>0:
                        do = 'SAME'
                else:
                    do = opt[i]
            o.Draw(do)
    else:
        print dobj.__class__
        print dobj[0].__class__
        print 'Class not recognized'
        raise


    CMS_lumi.CMS_lumi(c, -1, iPosCMS)

    if makeLegend:
        leg = rt.TLegend(legPos[0], legPos[1], legPos[2], legPos[3])
        leg.SetBorderSize(0)
        leg.SetFillStyle(0)
        leg.SetTextFont(42)
        leg.SetTextAlign(12)

        for i,o in enumerate(dobj):
            tag = dobj.GetName() if legNames is None else legNames[i]
            leg.AddEntry(o, tag, 'lp')

        c.leg= leg
        c.leg.Draw('same')

    c.obj = dobj
    if drawCanvas:
        c.Draw()
    return c

def extarct(t, branches = []):
    if len(branches) == 0:
        branches = t.keys()
    l = {}
    for k in branches:
#         print 'Loading branch', k
        m = []
        for i, e in enumerate(t.array(k)):
            if e.__class__ == np.float32:
                m += [e]
            else:
                m += list(e)
        l[k] = np.array(m)

    return l

def extarct_multiple(fname, branches = [], flag=''):
    if len(branches) == 0:
        print 'Must give a branches list'
    l = {}
    for b in branches:
        l[b] = []

    if not isinstance(fname, list):
        flist = glob(fname)
    else:
        flist = fname

    for i,f in enumerate(flist):
        try:
            t = ur.open(f)
            if 'outA;1' in t.keys():
                t=t['outA']['Tevts']
                for k in branches:
                    if flag=='data' and k[:2] == 'MC':
                        continue
                    if not (k in t.keys()):
                        continue
                    for i, e in enumerate(t.array(k)):
                        try:
                            l[k] += list(e)
                        except:
                            l[k] += [e]
        except:
            print 'Error in file:', f

    for b in branches:
        l[b] = np.array(l[b])
    return l

def createSel(d, cut):
    sel = np.ones_like(d[cut.keys()[0]], dtype=bool)
    for k, v in cut.iteritems():
        sel = np.logical_and(sel, ops[v[0]](d[k], v[1]) )
    return sel

def getEff(k,N):
    e = k/float(N)
    de = np.sqrt(e*(1-e)/N)
    return [e, de]

class FileLock:
    def __init__(self, filename):
        self.filename = filename
        self.fd = None
        self.pid = os.getpid()

    def acquire(self):
        try:
            self.fd = os.open(self.filename, os.O_CREAT|os.O_EXCL|os.O_RDWR)
            # Only needed to let readers know who's locked the file
            os.write(self.fd, "%d" % self.pid)
            return 1    # return ints so this can be used in older Pythons
        except OSError:
            self.fd = None
            return 0

    def release(self):
        if not self.fd:
            return 0
        try:
            os.close(self.fd)
            os.remove(self.filename)
            return 1
        except OSError:
            return 0

    def __del__(self):
        self.release()

def load_data(filename,stop=None,branches=None,cache_path='/storage/af/group/rdst_analysis',verbose=False):
    """
    Returns a pandas dataframe of the skimmed data in `filename`. Caches the
    result in a local .cache directory so that subsequent calls are fast.
    """
    import hashlib
    mtime = os.path.getmtime(filename)
    aux = abspath(filename) + str(mtime) + str(stop)
    if branches is not None:
        aux += ''.join(branches)
    sha1 = hashlib.sha1(aux).hexdigest()
    key = "%s.hdf5" % sha1
    filepath = join(cache_path,".cache","combine",key)
    lockpath = join(cache_path,".cache","combine",key + ".lock")
    dirname = os.path.dirname(filepath)
    if not exists(dirname):
        os.makedirs(dirname)
    lock = FileLock(lockpath)
    nwait = 0
    while True:
        if nwait > 100:
            raise Exception("waited too long for %s" % filepath)
        if lock.acquire():
            # got it
            if exists(filepath):
                try:
                    if verbose:
                        print 'Loading from cache', filename
                    return pd.read_hdf(filepath,'df')
                except EOFError:
                    pass
            ds = pd.DataFrame(rtnp.root2array(filename,stop=stop,branches=branches))
            reType = {}
            for colName in ds.columns:
                reType[colName] = np.float32
            ds = ds.astype(reType)
            if verbose:
                print 'Dumping', filename, 'as', filepath
            ds.to_hdf(filepath,'df',mode='w')
            return ds
        else:
            print("failed to acquire lock for %s. Waiting 10 seconds..." % filepath)
            time.sleep(10)
            nwait += 1

class DSetLoader(object):
    def __init__(self, in_sample,
                 # candLoc='/storage/af/user/ocerri/BPhysics/data/cmsMC_private/',
                 candLoc='/storage/af/group/rdst_analysis/BPhysics/data/cmsMC/',
                 candDir='ntuples_B2DstMu',
                 # site_loc_conf = '/storage/cms/store/user/ocerri/',
                 sampleFile = join(expanduser("~"),"RDstAnalysis/CMSSW_10_2_3/src/ntuplizer/BPH_RDntuplizer/production/samples.yml"),
                 skim_tag = '',
                 loadSkim=None
                 ):
        samples = load_yaml(sampleFile)['samples']
        if not in_sample in samples.keys():
            raise
        self.sample = in_sample
        self.candLoc = candLoc
        self.candDir = candDir

        # self.MINIAOD_dirs = []
        # for part in samples[self.sample]['parts']:
        #     aux = glob(part)
        #     if len(aux) > 0:
        #         aux = [os.path.dirname(part)]
        #     else:
        #         aux = glob(site_loc_conf + part[:-38].replace('ocerri-','') + '/*/*')
        #     self.MINIAOD_dirs += aux

        aux = os.path.dirname(sampleFile) + '/inputFiles_' + samples[self.sample]['dataset'] + '.txt'
        fAux = open(aux, 'r')
        self.MINIAOD_filelist = [ x[:-1]for x in fAux.readlines()]

        self.full_name = samples[self.sample]['dataset']

        res = glob(os.path.join(self.candLoc, self.full_name, self.candDir))
        if res:
            self.ntuples_dir = res[0]
            self.skimmed_dir = os.path.join(self.ntuples_dir, 'skimmed') + skim_tag
        else:
            self.ntuples_dir = ''
            self.skimmed_dir = ''

        effMCgenFile = os.path.join(self.candLoc, self.full_name, 'effMCgenerator.yaml')
        if os.path.isfile(effMCgenFile):
            self.effMCgen = load_yaml(effMCgenFile)

        effCandFile = os.path.join(self.ntuples_dir, 'effCAND.yaml')
        if os.path.isfile(effCandFile):
            self.effCand = load_yaml(effCandFile)
        else:
            print 'CAND efficiency file missing for {}.'.format(in_sample)

        if not loadSkim is None:
            if loadSkim == 'all':
                for cat in ['Low', 'Mid', 'High']: self.getSkimmedData(cat)
            else: self.getSkimmedData(loadSkim)


    def getSkimmedData(self, catName , tag='corr'):
        loc = self.skimmed_dir + '/{}_{}.root'.format(catName, tag)
        if not hasattr(self, 'data'):
            self.data = {}
        self.data['{}_{}'.format(catName, tag)] = load_data(loc)
        return

    def getSkimEff(self, catName='probe'):
        if catName is 'probe':
            with open(self.skimmed_dir + '/selTree.log', 'r') as f:
                aux = f.readlines()[-1][:-1].split(' ')
                return [float(aux[1])*1e-2, float(aux[3])*1e-2]
        else:
            with open(self.skimmed_dir + '/{}.log'.format(catName), 'r') as f:
                aux = f.readlines()[-1][:-1].split(' ')
                return [float(aux[1])*1e-2, float(aux[3])*1e-2]

    def printSkimEffLatex(self, catName):
        r, dr = self.getSkimEff(catName)
        return '${:.2f} \\pm {:.2f}$'.format(r*100, dr*100)

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 'on', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'off', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')
