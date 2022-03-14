# Analysis code for the R(D*) measurement with parked data in CMSSW

This code has to be run after sourceing CMSSW_10_2_3. It does not require compilation but needs the CMSSW environment to be active.

## Suggestions for installation

```
cd ~
mkdir RDstAnalysis
cd RDstAnalysis

cmsrel CMSSW_10_2_3
cd CMSSW_10_2_3/src
cmsenv
cd ../..

git clone https://github.com/ocerri/BPH_RD_Analysis
```

# Setting up Combine

To set up Combine you can run the following commands (see [https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/](https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/))

```
cd ~/RDstAnalysis
export SCRAM_ARCH=slc7_amd64_gcc700
cmsrel CMSSW_10_2_13
cd CMSSW_10_2_13/src
cmsenv
git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
cd HiggsAnalysis/CombinedLimit
cd $CMSSW_BASE/src/HiggsAnalysis/CombinedLimit
git fetch origin
git checkout v8.2.0
scramv1 b clean; scramv1 b -j12 # always make a clean build
```

Before running Combine, make sure to run:

```
source ~/RDstAnalysis/CMSSW_10_2_13; cmsenv
```

# Utilities

### Notebook extensions

To install notebook extensions:
``pip install jupyter_contrib_nbextensions``
``jupyter contrib nbextension install --user``
In case config parser have erros try to install the specific release
``pip install --user configparser==3.5.1``
