# For some reason in the singularity images, the $HOME environment variable
# gets set to /wntmp/localusers/[username]. I haven't been able to figure out
# why, but to fix this issue, we set the environment variable $BPH_RD_ANALYSIS
# when running jobs in singularity via condor to be the BPH_RD_Analysis
# directory.
if [[ -z "${BPH_RD_ANALYSIS}" ]]; then
    export PYTHONPATH=$HOME/RDstAnalysis/BPH_RD_Analysis/lib:$PYTHONPATH
    export PYTHONPATH=$HOME/RDstAnalysis/BPH_RD_Analysis/analysis:$PYTHONPATH
else
    export PYTHONPATH=${BPH_RD_ANALYSIS}/lib:$PYTHONPATH
    export PYTHONPATH=${BPH_RD_ANALYSIS}/analysis:$PYTHONPATH
fi
