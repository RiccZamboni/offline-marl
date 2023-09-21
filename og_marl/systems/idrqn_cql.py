"""Implementation of IDRQN+CQL"""
import tensorflow as tf
import sonnet as snt

from og_marl.systems.qmix import QMIXSystem
from og_marl.utils import (
    gather,
    batch_concat_agent_id_to_obs,
    switch_two_leading_dims,
    merge_batch_and_agent_dim_of_time_major_sequence,
    expand_batch_and_agent_dim_of_time_major_sequence,
    set_growing_gpu_memory,
    dict_to_tensor,
)

set_growing_gpu_memory()


class IDRQNCQLSystem(QMIXSystem):
    """IDRQN+CQL System"""

    def __init__(
        self,
        environment,
        logger,
        num_ood_actions=5,
        cql_weight=1.0,
        linear_layer_dim=100,
        recurrent_layer_dim=100,
        mixer_embed_dim=64,
        mixer_hyper_dim=32,
        batch_size=64,
        discount=0.99,
        target_update_rate=0.005,
        learning_rate=3e-4,
        add_agent_id_to_obs=False,
    ):

        super().__init__(
            environment,
            logger,
            linear_layer_dim=linear_layer_dim,
            recurrent_layer_dim=recurrent_layer_dim,
            mixer_embed_dim=mixer_embed_dim,
            mixer_hyper_dim=mixer_hyper_dim,
            add_agent_id_to_obs=add_agent_id_to_obs,
            batch_size=batch_size,
            discount=discount,
            target_update_rate=target_update_rate,
            learning_rate=learning_rate
        )

        # CQL
        self._num_ood_actions = num_ood_actions
        self._cql_weight = cql_weight

    @tf.function(jit_compile=True)
    def _tf_train_step(self, batch):
        batch = dict_to_tensor(self._environment._agents, batch)


        # Unpack the batch
        observations = batch.observations # (B,T,N,O)
        actions = batch.actions # (B,T,N,A)
        legal_actions = batch.legal_actions # (B,T,N,A)
        env_states = batch.env_state # (B,T,S)
        rewards = batch.rewards # (B,T,N)
        done = batch.done # (B,T)
        zero_padding_mask = batch.zero_padding_mask # (B,T)

        # Get dims
        B, T, N, A = legal_actions.shape

        # Maybe add agent ids to observation
        if self._add_agent_id_to_obs:
            observations = batch_concat_agent_id_to_obs(observations)

        # Make time-major
        observations = switch_two_leading_dims(observations)

        # Merge batch_dim and agent_dim
        observations = merge_batch_and_agent_dim_of_time_major_sequence(observations)

        # Unroll target network
        target_qs_out, _ = snt.static_unroll(
            self._target_q_network, 
            observations,
            self._target_q_network.initial_state(B*N)
        )

        # Expand batch and agent_dim
        target_qs_out = expand_batch_and_agent_dim_of_time_major_sequence(target_qs_out, B, N)

        # Make batch-major again
        target_qs_out = switch_two_leading_dims(target_qs_out)

        with tf.GradientTape() as tape:
            # Unroll online network
            qs_out, _ = snt.static_unroll(
                self._q_network, 
                observations, 
                self._q_network.initial_state(B*N)
            )

            # Expand batch and agent_dim
            qs_out = expand_batch_and_agent_dim_of_time_major_sequence(qs_out, B, N)

            # Make batch-major again
            qs_out = switch_two_leading_dims(qs_out)

            # Pick the Q-Values for the actions taken by each agent
            chosen_action_qs = gather(qs_out, actions, axis=3, keepdims=False)

            # Max over target Q-Values/ Double q learning
            qs_out_selector = tf.where(
                tf.cast(legal_actions, "bool"), qs_out, -9999999
            )  # legal action masking
            cur_max_actions = tf.argmax(qs_out_selector, axis=3)
            target_max_qs = gather(target_qs_out, cur_max_actions, axis=-1)

            # Compute targets
            targets = rewards[:, :-1] + tf.expand_dims((1-done[:, :-1]), axis=-1) * self._discount * target_max_qs[:, 1:]
            targets = tf.stop_gradient(targets)

            # TD-Error Loss
            loss = 0.5 * tf.square(targets - chosen_action_qs[:, :-1])

            #############
            #### CQL ####
            #############

            random_ood_actions = tf.random.uniform(
                                shape=(self._num_ood_actions, B, T, N),
                                minval=0,
                                maxval=A,
                                dtype=tf.dtypes.int64
            ) # [Ra, B, T, N]

            all_ood_qs = []
            for i in range(self._num_ood_actions):
                # Gather
                one_hot_indices = tf.one_hot(random_ood_actions[i], depth=qs_out.shape[-1])
                ood_qs = tf.reduce_sum(
                    qs_out * one_hot_indices, axis=-1, keepdims=False
                ) # [B, T, N]

                # Mixing
                all_ood_qs.append(ood_qs) # [B, T, Ra]

            all_ood_qs.append(chosen_action_qs) # [B, T, Ra + 1]
            all_ood_qs = tf.concat(all_ood_qs, axis=-1)

            cql_loss = self._apply_mask(tf.reduce_logsumexp(all_ood_qs, axis=-1, keepdims=True)[:, :-1], zero_padding_mask) - self._apply_mask(chosen_action_qs[:, :-1], zero_padding_mask)

            #############
            #### end ####
            #############

            # Mask out zero-padded timesteps
            loss = self._apply_mask(loss, zero_padding_mask) + cql_loss

        # Get trainable variables
        variables = (
            *self._q_network.trainable_variables,
        )

        # Compute gradients.
        gradients = tape.gradient(loss, variables)

        # Apply gradients.
        self._optimizer.apply(gradients, variables)

        # Online variables
        online_variables = (
            *self._q_network.variables,
        )

        # Get target variables
        target_variables = (
            *self._target_q_network.variables,
        )

        # Maybe update target network
        self._update_target_network(online_variables, target_variables)

        return {
            "Loss": loss,
            "Mean Q-values": tf.reduce_mean(qs_out),
            "Mean Chosen Q-values": tf.reduce_mean(chosen_action_qs),
        }