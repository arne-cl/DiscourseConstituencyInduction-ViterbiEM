#!/usr/bin/env sh

STORAGE=$1

java edu.stanford.nlp.process.PTBTokenizer \
    --ioFileList ${STORAGE}/ptbwsj_wo_rstdt/tmp.preprocessing/filelist.ptbtokenizer2.txt \
    --preserveLines \
    --options "normalizeSpace=false"

