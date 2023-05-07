#!/bin/bash 

trap "exit" INT TERM ERR
trap "kill 0" EXIT

for JOB_IDX in {0..3}
do
    python run_gridworld.py $JOB_IDX 4 dm 16 &
done

wait
