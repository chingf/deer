import sys
import logging
import pickle
import yaml
from joblib import Parallel, delayed
import numpy as np
import re
import matplotlib.pyplot as plt
import os
import time
import shortuuid
from flatten_dict import flatten
from flatten_dict import unflatten
import torch

from acme import specs
from acme import wrappers

from auxrl.Agent import Agent
from auxrl.networks.Network import Network
from auxrl.environments.GridWorld import Env as Env
from auxrl.utils import run_train_episode, run_eval_episode, run_modified_transition
from model_parameters.gridworld import *
from model_parameters.dicarlo_swap import *

"""
Simulates the Nuo/Dicarlo swap experiment with gridworld environment and
shuffled observations. Initialized with network trained on gridworld. Goal is
not moved, but transition statistics are changed for user-specified locations.
These locations are hand-selected.
"""

# Command-line args
job_idx = int(sys.argv[1])
n_jobs = int(sys.argv[2])
nn_yaml = sys.argv[3]
internal_dim = int(sys.argv[4])

# Experiment Parameters
load_function = dswap
source_prefix = 'postbug_gridworld8x8_shuffobs'
source_suffix = ''
source_episode = 600
fname_prefix = f'dicarlo_swap_{source_prefix}'
fname_suffix = ''
n_episodes = 4
epsilon = 1.
eval_every = 1
save_net_every = 1
size_maze = 8
n_iters = 15

# Less changed args
swap_params = postbug_contig()
random_seed = True
shuffle = True
encoder_only = False
freeze_encoder = False
n_cpu_jobs = 56 # Only used in event of CPU paralellization

# If manual GPU setting
if len(sys.argv) > 5:
    gpu_override = str(sys.argv[5])
else:
    gpu_override = None

# CPU vs GPU
try:
    n_gpus = (len(os.environ['CUDA_VISIBLE_DEVICES'])+1)/2
except:
    n_gpus = 0

# Now set GPU
if n_gpus > 1:
    if gpu_override == None:
        device_num = str(job_idx % n_gpus)
    else:
        device_num = gpu_override
    my_env = os.environ
    my_env["CUDA_VISIBLE_DEVICES"] = device_num

# Make directories
if 'SLURM_JOBID' in os.environ.keys():
    engram_dir = '/mnt/smb/locker/aronov-locker/Ching/rl/' # Axon Path
else:
    engram_dir = '/home/cf2794/engram/Ching/rl/' # Cortex Path
exp_dir = f'{fname_prefix}_{nn_yaml}_dim{internal_dim}{fname_suffix}/'
source_dir = f'{source_prefix}_{nn_yaml}_dim{internal_dim}{source_suffix}/'
for d in ['pickles/', 'nnets/', 'figs/', 'params/']:
    os.makedirs(f'{engram_dir}{d}{exp_dir}', exist_ok=True)
pickle_dir = f'{engram_dir}pickles/{exp_dir}/'
param_dir = f'{engram_dir}params/{exp_dir}/'

def gpu_parallel(job_idx):
    for _arg in split_args[job_idx]:
        run(_arg)

def cpu_parallel():
    job_results = Parallel(n_jobs=n_cpu_jobs)(delayed(run)(arg) for arg in args)

def run(arg):
    _fname, source_fname, loss_weights, param_update, i = arg
    swap_param_key = f'{source_fname[len(source_prefix)+1:]}_{i}'
    if swap_param_key not in swap_params.keys():
        return
    _swap_params = swap_params[swap_param_key]
    print(_fname)
    print(f'Iteration {i}')
    print(loss_weights)

    source_nnet_dir = f'{engram_dir}nnets/{source_dir}'
    if source_fname is None:
        load_network = None
        prev_pos_goal = None
    else:
        source_fname_path = f'{source_nnet_dir}'
        source_fname_path += f'{source_fname}_{i}/'
        load_network = [f'{source_fname_path}', source_episode]

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(device)
    fname = f'{_fname}_{i}'
    fname_nnet_dir = f'{engram_dir}nnets/{exp_dir}/{fname}/'
    fname_fig_dir = f'{engram_dir}figs/{exp_dir}/{fname}/'
    fname_pickle_dir = f'{engram_dir}pickles/{exp_dir}/{fname}/'
    for _dir in [fname_nnet_dir, fname_fig_dir, fname_pickle_dir]:
        os.makedirs(_dir, exist_ok=True)

    net_exists = np.any(['network_ep' in f for f in os.listdir(fname_nnet_dir)])
    #if net_exists:
    #    print(f'Skipping {fname}')
    #    return
    #else:
    #    print(f'Running {fname}')

    parameters = {
        'source_network_path': load_network,
        'source_network_episode': source_episode,
        'encoder_only': encoder_only, 'freeze_encoder': freeze_encoder,
        'fname': fname,
        'n_episodes': n_episodes,
        'n_test_episodes': 5,
        'agent_args': {
            'loss_weights': loss_weights, 'lr': 1e-3,
            'replay_capacity': 100_000, 'epsilon': epsilon,
            'batch_size': 64, 'target_update_frequency': 1000,
            'train_seq_len': 1},
        'network_args': {
            'latent_dim': internal_dim, 'network_yaml': nn_yaml,
            'freeze_encoder': freeze_encoder},
        'dset_args': {
            'layout': size_maze, 'shuffle_obs': shuffle}
        }
    parameters = flatten(parameters)
    parameters.update(flatten(param_update))
    parameters = unflatten(parameters)
    with open(f'{param_dir}{_fname}.yaml', 'w') as outfile:
        yaml.dump(parameters, outfile, default_flow_style=False)
    if random_seed: np.random.seed(i)
    env = Env(**parameters['dset_args'])
    env = wrappers.SinglePrecisionWrapper(env)
    env_spec = specs.make_environment_spec(env)
    with open(f'{fname_nnet_dir}goal.txt', 'w') as goalfile:
        goalfile.write(str(env._goal_state))
    if parameters['dset_args']['shuffle_obs']:
        with open(f'{fname_nnet_dir}shuffle_indices.txt', 'w') as goalfile:
            goalfile.write(str(env._shuffle_indices))
    network = Network(env_spec, device=device, **parameters['network_args'])
    agent = Agent(env_spec, network, device=device, **parameters['agent_args'])

    # Load previously trained network
    try:
        if load_network is not None:
            print('Loading network')
            agent.load_network(load_network[0], load_network[1], encoder_only)
    except:
        print(f'ERROR LOADING {load_network[0]}, {load_network[1]}')
        return

    # Define environment swap position
    env.swap_transitions(_swap_params)

    os.makedirs(fname_nnet_dir, exist_ok=True)
    result = {}
    result['episode'] = []
    result['step'] = []
    result['train_loss'] = []
    result['mf_loss'] = []
    result['neg_random_loss'] = []
    result['neg_neighbor_loss'] = []
    result['pos_sample_loss'] = []
    result['train_score'] = []
    result['valid_score'] = []
    result['train_steps_per_ep'] = []
    result['valid_steps_per_ep'] = []
    result['model'] = []
    result['model_iter'] = []

    sec_per_step_SUM = 0.
    sec_per_step_NUM = 0.

    for episode in range(n_episodes):
        start = time.time()
        losses, score, steps_per_episode = run_modified_transition(
            env, agent, max_timestep=2)
        end = time.time()
        sec_per_step_SUM += (end-start)
        sec_per_step_NUM += steps_per_episode
        result['episode'].append(episode)
        result['step'].append(sec_per_step_NUM)
        result['train_loss'].append(losses[4])
        result['mf_loss'].append(losses[3])
        result['neg_random_loss'].append(losses[2])
        result['neg_neighbor_loss'].append(losses[1])
        result['pos_sample_loss'].append(losses[0])
        result['train_score'].append(score)
        result['train_steps_per_ep'].append(steps_per_episode)
        result['model'].append(_fname)
        result['model_iter'].append(i)
        if episode % eval_every == 0:
            sec_per_step = sec_per_step_SUM/sec_per_step_NUM
            print(f'[TRAIN SUMMARY] {500*sec_per_step} sec/ 500 steps')
            print(f'{sec_per_step_NUM} training steps elapsed.')
            score, steps_per_episode = run_eval_episode(
                env, agent, parameters['n_test_episodes'])
            result['valid_score'].append(score)
            result['valid_steps_per_ep'].append(steps_per_episode)
            # Save plots tracking training progress
            fig, axs = plt.subplots(3, 2, figsize=(7, 10))
            loss_keys = [
                'train_loss', 'mf_loss', 'neg_random_loss',
                'neg_neighbor_loss', 'pos_sample_loss']
            for i, loss_key in enumerate(loss_keys):
                ax = axs[i%3][i//3]
                ax.plot(result[loss_key])
                ax.set_ylabel(loss_key)
            plt.tight_layout()
            plt.savefig(f'{fname_fig_dir}train_losses.png')
            plt.figure()
            plt.plot(result['train_score'])
            plt.ylabel('Training Score'); plt.xlabel('Training Episodes')
            plt.tight_layout()
            plt.savefig(f'{fname_fig_dir}train_scores.png')
            plt.figure()
            plt.plot(
                result['episode'][::eval_every],
                result['valid_score'][::eval_every])
            plt.ylabel('Validation Score'); plt.xlabel('Training Episodes')
            plt.tight_layout()
            plt.savefig(f'{fname_fig_dir}valid_scores.png')
            plt.figure()
            plt.plot(
                result['episode'][::eval_every],
                result['valid_steps_per_ep'][::eval_every])
            plt.ylabel('Validation Steps to Goal'); plt.xlabel('Training Episodes')
            plt.tight_layout()
            plt.savefig(f'{fname_fig_dir}valid_steps.png')
            plt.close('all')
        else:
            result['valid_score'].append(None)
            result['valid_steps_per_ep'].append(None)
        if episode % save_net_every == 0:
            agent.save_network(fname_nnet_dir, episode)

    # Save pickle
    unique_id = shortuuid.uuid()
    with open(f'{pickle_dir}{unique_id}.p', 'wb') as f:
        pickle.dump(result, f)

# Load model parameters
fname_grid, loss_weights_grid, param_updates = load_function()
source_fnames = [f'{source_prefix}_{f}' for f in fname_grid]
fname_grid = [f'{fname_prefix}_{f}' for f in fname_grid]

# Collect argument combinations
iters = np.arange(n_iters)
args = []
for arg_idx in range(len(fname_grid)):
    for i in iters:
        fname = fname_grid[arg_idx]
        source_fname = source_fnames[arg_idx]
        loss_weights = loss_weights_grid[arg_idx]
        param_update = param_updates[arg_idx]
        args.append([fname, source_fname, loss_weights, param_update, i])
split_args = np.array_split(args, n_jobs)

import time
start = time.time()
# Run relevant parallelization script
if job_idx == -1:
    cpu_parallel()
else:
    gpu_parallel(job_idx)
end = time.time()

print(f'ELAPSED TIME: {end-start} seconds')
