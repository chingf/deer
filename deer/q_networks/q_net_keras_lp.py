"""
Code for general deep Q-learning using Keras that can take as inputs scalars, vectors and matrices

.. Author: Vincent Francois-Lavet
"""

import numpy as np
np.set_printoptions(threshold=np.nan)
from keras.optimizers import SGD,RMSprop
from keras import backend as K
from ..base_classes import QNetwork
from .NN_keras_lp import NN # Default Neural network used

class MyQNetwork(QNetwork):
    """
    Deep Q-learning network using Keras (with any backend)
    
    Parameters
    -----------
    environment : object from class Environment
    rho : float
        Parameter for rmsprop. Default : 0.9
    rms_epsilon : float
        Parameter for rmsprop. Default : 0.0001
    momentum : float
        Default : 0
    clip_delta : float
        Not implemented.
    freeze_interval : int
        Period during which the target network is freezed and after which the target network is updated. Default : 1000
    batch_size : int
        Number of tuples taken into account for each iteration of gradient descent. Default : 32
    update_rule: str
        {sgd,rmsprop}. Default : rmsprop
    random_state : numpy random number generator
    double_Q : bool, optional
        Activate or not the double_Q learning.
        More informations in : Hado van Hasselt et al. (2015) - Deep Reinforcement Learning with Double Q-learning.
    neural_network : object, optional
        default is deer.qnetworks.NN_keras
    """

    def __init__(self, environment, rho=0.9, rms_epsilon=0.0001, momentum=0, clip_delta=0, freeze_interval=1000, batch_size=32, update_rule="rmsprop", random_state=np.random.RandomState(), double_Q=False, neural_network=NN):
        """ Initialize environment
        
        """
        QNetwork.__init__(self,environment, batch_size)

        
        self._rho = rho
        self._rms_epsilon = rms_epsilon
        self._momentum = momentum
        self._update_rule = update_rule
        #self.clip_delta = clip_delta
        self._freeze_interval = freeze_interval
        self._double_Q = double_Q
        self._random_state = random_state
        self.update_counter = 0    
        self.d_loss=1.

                
        Q_net = neural_network(self._batch_size, self._input_dimensions, self._n_actions, self._random_state)

        optimizer=RMSprop(lr=0.00005, rho=0.9, epsilon=1e-06)
        optimizer2=RMSprop(lr=0.0001, rho=0.9, epsilon=1e-06)#.Adam(lr=0.0002, beta_1=0.5, beta_2=0.999, epsilon=1e-08)

        self.encoder = Q_net.encoder_model()

        self.q_vals, self.params = Q_net._buildDQN(self.encoder)

        self.generator_transition = Q_net.generator_transition_model(self.encoder)
        self.discriminator = Q_net.discriminator_model()
        self.full_trans = Q_net.full_model_trans(self.generator_transition, self.encoder, self.discriminator)
        self.full_enc = Q_net.full_model_enc(self.encoder, self.discriminator)

        self.discriminator.trainable = True
        self.discriminator.compile(loss='binary_crossentropy', optimizer=optimizer2)
        self.generator_transition.compile(optimizer=optimizer,
                  loss='mae',
                  metrics=['accuracy'])
        self.encoder.compile(optimizer=optimizer,
                  loss='mae',
                  metrics=['accuracy'])
        self.full_trans.compile(loss='binary_crossentropy', optimizer=optimizer)
        self.full_enc.compile(loss='binary_crossentropy', optimizer=optimizer)
            
        self._compile()

        self.next_q_vals, self.next_params = Q_net._buildDQN(self.encoder)
        self.next_q_vals.compile(optimizer='rmsprop', loss='mse') #The parameters do not matter since training is done on self.q_vals

        self._resetQHat()

    def getAllParams(self):
        params_value=[]
        for i,p in enumerate(self.params):
            params_value.append(K.get_value(p))
        return params_value

    def setAllParams(self, list_of_values):
        for i,p in enumerate(self.params):
            K.set_value(p,list_of_values[i])

    def train(self, states_val, actions_val, rewards_val, next_states_val, terminals_val):
        """
        Train one batch.

        1. Set shared variable in states_shared, next_states_shared, actions_shared, rewards_shared, terminals_shared         
        2. perform batch training

        Parameters
        -----------
        states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
        actions_val : b x 1 numpy array of integers
        rewards_val : b x 1 numpy array
        next_states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
        terminals_val : b x 1 numpy boolean array

        Returns
        -------
        Average loss of the batch training (RMSE)
        Individual (square) losses for each tuple
        """
        
        #print "self.discriminator.get_weights()"
        #print self.discriminator.get_weights()

        noise = np.random.uniform(-1,1,size=(self._batch_size,5)) #self._rand_vect_size=5
        ##print "[states_val[0],actions_val,noise]"
        ##print [states_val[0],actions_val,noise]
        ##print "states_val.tolist()"
        ##print states_val.tolist()
        onehot_actions = np.zeros((self._batch_size, self._n_actions))
        onehot_actions[np.arange(self._batch_size), actions_val[:,0]] = 1
        #print onehot_actions
        #print "[states_val[0],onehot_actions,noise]"
        #print [states_val[0],onehot_actions,noise]
        ETs=self.generator_transition.predict([states_val[0],onehot_actions,noise])
        Es_=self.encoder.predict([next_states_val[0]])
        Es=self.encoder.predict([states_val[0]])
        
        
        X = np.concatenate((ETs, Es_))
        if(self.update_counter%100==0):
            print states_val[0][0]
            print next_states_val[0][0]
            print actions_val, rewards_val, terminals_val
            print "Es"
            print Es
            print "X"
            print ETs,Es_
            #print "disc"
            #print self.discriminator.predict([X, np.tile(onehot_actions,(2,1))])
            print "full trans"
            print self.full_trans.predict([states_val[0], onehot_actions, noise, Es])
            print "full enc"
            print self.full_enc.predict([next_states_val[0], onehot_actions, Es])
            
        y = [1] * self._batch_size + [0] * self._batch_size # first batch size is ETs and second is Es'
        
        noise_to_avoid_too_easy_disc=np.random.normal(size=X.shape)*(0.7-min(self.d_loss,0.7))*1#*100/max(100,epoch)
        self.d_loss=0
        for i in range(5):
            self.discriminator.trainable = True
            loss = self.discriminator.train_on_batch([X+noise_to_avoid_too_easy_disc, np.tile(Es,(2,1)), np.tile(onehot_actions,(2,1))], y)
            self.d_loss += loss
            if loss < 0.01:
                break
        self.d_loss=self.d_loss/(i+1)

        if(self.update_counter%100==0):
            print "d_loss"
            print self.d_loss
                    
        # Training generator ETs
        self.discriminator.trainable = False # required?
        g_loss1 = self.full_trans.train_on_batch([states_val[0], onehot_actions, noise, Es], [0] * self._batch_size) # ETs should look like Es_

        # Training generator Es'
        g_loss2 = self.full_enc.train_on_batch([next_states_val[0], onehot_actions, Es], [1] * self._batch_size) # Es_ should look like ETs

        if(self.update_counter%100==0):
            print "g_losses"
            print g_loss1
            print g_loss2


        if self.update_counter % self._freeze_interval == 0:
            self._resetQHat()
        
        next_q_vals = self.next_q_vals.predict(next_states_val.tolist())
        
        if(self._double_Q==True):
            next_q_vals_current_qnet=self.q_vals.predict(next_states_val.tolist())
            argmax_next_q_vals=np.argmax(next_q_vals_current_qnet, axis=1)
            max_next_q_vals=next_q_vals[np.arange(self._batch_size),argmax_next_q_vals].reshape((-1, 1))
        else:
            max_next_q_vals=np.max(next_q_vals, axis=1, keepdims=True)

        not_terminals=np.ones_like(terminals_val) - terminals_val
        
        target = rewards_val + not_terminals * self._df * max_next_q_vals.reshape((-1))
        
        q_vals=self.q_vals.predict(states_val.tolist())

        # In order to obtain the individual losses, we predict the current Q_vals and calculate the diff
        q_val=q_vals[np.arange(self._batch_size), actions_val.reshape((-1,))]#.reshape((-1, 1))        
        diff = - q_val + target 
        loss_ind=pow(diff,2)
                
        q_vals[  np.arange(self._batch_size), actions_val.reshape((-1,))  ] = target
                
        # Is it possible to use something more flexible than this? 
        # Only some elements of next_q_vals are actual value that I target. 
        # My loss should only take these into account.
        # Workaround here is that many values are already "exact" in this update
        if (self.update_counter<10000):
            loss=self.q_vals.train_on_batch(states_val.tolist() , q_vals ) 
                
        if(self.update_counter%100==0):
            print self.update_counter
        
        self.update_counter += 1        

        # loss*self._n_actions = np.average(loss_ind)
        return np.sqrt(loss),loss_ind


    def qValues(self, state_val):
        """ Get the q values for one belief state

        Arguments
        ---------
        state_val : one belief state

        Returns
        -------
        The q values for the provided belief state
        """ 
        return self.q_vals.predict([np.expand_dims(state,axis=0) for state in state_val])[0]

    def chooseBestAction(self, state):
        """ Get the best action for a belief state

        Arguments
        ---------
        state : one belief state

        Returns
        -------
        The best action : int
        """        
        q_vals = self.qValues(state)

        return np.argmax(q_vals),np.max(q_vals)
        
    def _compile(self):
        """ compile self.q_vals
        """
        if (self._update_rule=="sgd"):
            optimizer = SGD(lr=self._lr, momentum=self._momentum, nesterov=False)
        elif (self._update_rule=="rmsprop"):
            optimizer = RMSprop(lr=self._lr, rho=self._rho, epsilon=self._rms_epsilon)
        else:
            raise Exception('The update_rule '+self._update_rule+' is not implemented.')
        
        self.q_vals.compile(optimizer=optimizer, loss='mse')

    def _resetQHat(self):
        for i,(param,next_param) in enumerate(zip(self.params, self.next_params)):
            K.set_value(next_param,K.get_value(param))