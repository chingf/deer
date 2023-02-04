import numpy as np
import torch
from ..base_classes import Policy


class EpsilonGreedyPolicy(Policy):
    """The policy acts greedily with probability :math:`1-\epsilon` and acts randomly otherwise.
    It is now used as a default policy for the neural agent.

    Parameters
    -----------
    epsilon : float
        Proportion of random steps
    """
    def __init__(
        self, learning_algo, n_actions, random_state, epsilon,
        consider_valid_transitions=False
        ):
        Policy.__init__(self, learning_algo, n_actions, random_state)
        self._epsilon = epsilon
        self._consider_valid_transitions = consider_valid_transitions

    def action(self, state, mode=None, *args, **kwargs):
        if self.random_state.rand() < self._epsilon:
            if self._consider_valid_transitions:
                action, V = self.randomValidAction(state)
            else:
                action, V = self.randomAction()
        else:
            action, V = self.bestAction(state, mode, *args, **kwargs)

        return action, V

    def setEpsilon(self, e):
        """ Set the epsilon used for :math:`\epsilon`-greedy exploration
        """
        self._epsilon = e

    def epsilon(self):
        """ Get the epsilon for :math:`\epsilon`-greedy exploration
        """
        return self._epsilon

    def randomValidAction(self, state):
        """ Select random action, weighting actions by how far they take
        you in encoding space """

        # Get possible transition states over all actions
        state = torch.as_tensor(state, device=self.device).float()
        Es = self.crar.encoder(state)
        Es = [torch.clone(Es) for _ in range(self.n_actions)]
        Es = torch.stack(Es)
        onehot_actions = np.zeros((self.n_actions, self.n_actions))
        onehot_actions[np.arange(self.n_actions), np.arange(self.n_actions)] = 1
        onehot_actions = torch.as_tensor(
            onehot_actions, device=learning_algo.device).float()
        Es_and_actions = torch.cat([Es, onehot_actions], dim=1)
        with torch.no_grad():
            TEs = self.learning_algo.crar.transition(Es_and_actions)

        # Weight actions by predicted distance in encoding space
        actions = np.arange(TEs.shape[0])
        p = np.linalg.norm((TEs - Es).cpu().numpy(), axis=1)
        p = softmax(p, tau=10)
        action = np.random.choice(actions, p=p)
        V = 0
        return action, V

def softmax(x, tau=1):
    """Compute softmax values for each sets of scores in x."""
    return np.exp(x*tau) / np.sum(np.exp(x*tau), axis=0)
