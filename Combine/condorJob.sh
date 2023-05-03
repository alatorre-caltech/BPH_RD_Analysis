#!/bin/bash
home=$1
directory=$2
command="${@:3}"

export HOME=$home
cd $home/RDstAnalysis/CMSSW_10_2_13/src/
source /cvmfs/cms.cern.ch/cmsset_default.sh
eval `scramv1 runtime -sh`
echo "cd $directory"
cd $directory
echo "source ../env.sh"
source ../env.sh
echo "$command"
echo " "; echo " "; echo " "
echo "======================== JOB START ========================"
$command
exitcode=$?
echo " "; echo " "; echo " "
echo "======================== JOB DONE ========================"
echo "Exit code: $exitcode"
exit $exitcode
