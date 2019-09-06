# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SimPLe helper functions."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
import os
import random

from absl import logging
import cloudpickle as pickle
import numpy as np
from tensor2tensor.envs import env_problem_utils
from tensor2tensor.envs import trajectory
from tensorflow.io import gfile


def load_trajectories(trajectory_dir, eval_frac):
  """Loads trajectories from a possibly nested directory of pickles."""
  train_trajectories = []
  eval_trajectories = []
  # Search the entire directory subtree for trajectories.
  for (subdir, _, filenames) in gfile.walk(trajectory_dir):
    for filename in filenames:
      shard_path = os.path.join(subdir, filename)
      with gfile.GFile(shard_path, "rb") as f:
        trajectories = pickle.load(f)
        pivot = int(len(trajectories) * (1 - eval_frac))
        train_trajectories.extend(trajectories[:pivot])
        eval_trajectories.extend(trajectories[pivot:])
  assert train_trajectories, "Haven't found any training data."
  assert eval_trajectories, "Haven't found any evaluation data."
  return (train_trajectories, eval_trajectories)


def generate_examples(trajectories, trajectory_to_training_examples_fn):
  """Generates an infinite stream of shuffled examples out of trajectories."""
  examples = [
      example  # pylint: disable=g-complex-comprehension
      for trajectory_examples in map(
          trajectory_to_training_examples_fn, trajectories)
      for example in trajectory_examples
  ]
  assert examples
  while True:
    random.shuffle(examples)
    for example in examples:
      yield example


def mix_streams(stream1, stream2, mix_prob):
  """Mixes two streams together with a fixed probability."""
  while True:
    # In the corner cases (mix_prob = 0 or 1) mixing the other stream never
    # happens, because random() samples from the semi-open interval [0, 1).
    if random.random() < mix_prob:
      yield next(stream1)
    else:
      yield next(stream2)


def batch_stream(stream, batch_size):
  """Batches a stream of training examples."""
  def make_batch(examples):
    """Stacks a structure of numpy arrays nested in lists/tuples."""
    assert examples
    if isinstance(examples[0], (list, tuple)):
      return type(examples[0])(
          make_batch([example[i] for example in examples])
          for i in range(len(examples[0]))
      )
    else:
      return np.stack(examples, axis=0)

  # Take consecutive batches from an infinite stream. This way there are no
  # incomplete batches. We might get duplicate examples in the same batch, but
  # that should be very rare.
  while True:
    yield make_batch(list(itertools.islice(stream, batch_size)))


# TODO(pkozakowski): This is mostly a simplified version of
# env_problem_utils.play_env_problem_with_policy, generalized to work with
# policies not being neural networks. Unify if possible.
def play_env_problem(env, policy):
  """Plays an EnvProblem using a given policy function."""
  trajectories = [trajectory.Trajectory() for _ in range(env.batch_size)]
  observations = env.reset()
  for (traj, observation) in zip(trajectories, observations):
    traj.add_time_step(observation=observation)

  done_so_far = np.array([False] * env.batch_size)
  while not np.all(done_so_far):
    padded_observations, _ = env.trajectories.observations_np(
        len_history_for_policy=None)
    actions = policy(padded_observations)
    (observations, rewards, dones, _) = env.step(actions)
    for (traj, observation, action, reward, done) in zip(
        trajectories, observations, actions, rewards, dones
    ):
      if not traj.done:
        traj.change_last_time_step(action=action)
        traj.add_time_step(
            observation=observation, raw_reward=reward, done=done)
      env.reset(indices=env_problem_utils.done_indices(dones))
    done_so_far = np.logical_or(done_so_far, dones)
  return trajectories


def calculate_observation_error(real_trajectories, sim_trajectories):
  """Calculates MSE of observations in two trajectories."""
  def padded_obs(traj, length_difference):
    return np.pad(
        traj.observations_np,
        pad_width=((0, max(length_difference, 0)), (0, 0)),
        mode="edge",
    )

  def calculate_for_single_pair(real_trajectory, sim_trajectory):
    diff = sim_trajectory.num_time_steps - real_trajectory.num_time_steps
    padded_real_obs = padded_obs(real_trajectory, diff)
    padded_sim_obs = padded_obs(sim_trajectory, -diff)
    x = np.sum((padded_real_obs - padded_sim_obs) ** 2, axis=0)
    return x

  return np.mean([
      calculate_for_single_pair(real_traj, sim_traj)
      for (real_traj, sim_traj) in zip(real_trajectories, sim_trajectories)
  ], axis=0)


def plot_observation_error(real_trajectories, sim_trajectories, mpl_plt):
  """Plots observations from two trajectories on the same graph."""
  assert len(real_trajectories) == len(sim_trajectories)
  assert real_trajectories
  obs_dim = real_trajectories[0].last_time_step.observation.shape[0]
  (w, h) = mpl_plt.rcParams["figure.figsize"]
  ncols = len(real_trajectories)
  nrows = obs_dim
  (_, axes) = mpl_plt.subplots(
      nrows, ncols, figsize=(w * ncols, h * nrows))
  for (traj_index, (real_traj, sim_traj)) in enumerate(
      zip(real_trajectories, sim_trajectories)
  ):
    for dim_index in range(obs_dim):
      for (traj, label) in ((real_traj, "real"), (sim_traj, "simulated")):
        obs = traj.observations_np
        ax = axes[dim_index, traj_index]
        ax.plot(np.arange(obs.shape[0]), obs[:, dim_index], label=label)
        ax.legend()


def evaluate_model(sim_env, real_trajectories, mpl_plt, n_to_plot=3):
  """Reports the observation error metric and the corresponding plot."""
  if len(sim_env.observation_space.shape) != 1:
    logging.warning(
        "Could not evaluate the model - only environments with vector "
        "observation spaces are supported."
    )
    return

  assert len(real_trajectories) == sim_env.batch_size

  step = [0]
  def policy(observations):
    del observations
    def get_action(traj):
      if step[0] < traj.num_time_steps:
        return traj.time_steps[step[0]].action or 0
      else:
        return 0
    actions = np.array([get_action(traj) for traj in real_trajectories])
    step[0] += 1
    return actions

  sim_trajectories = play_env_problem(sim_env, policy)
  obs_errors = calculate_observation_error(real_trajectories, sim_trajectories)
  plot_observation_error(
      real_trajectories[:n_to_plot], sim_trajectories[:n_to_plot], mpl_plt)
  return {
      "observation_error/{}".format(i): obs_error
      for (i, obs_error) in enumerate(obs_errors)
  }