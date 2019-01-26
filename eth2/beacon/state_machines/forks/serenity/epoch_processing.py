from typing import (
    Tuple,
)

from eth2._utils.numeric import (
    is_power_of_two,
)
from eth2._utils.tuple import (
    update_tuple_item,
)
from eth2.beacon._utils.hash import (
    hash_eth2,
)
from eth2.beacon import helpers
from eth2.beacon.helpers import (
    get_active_validator_indices,
    get_attesting_validator_indices,
    get_crosslink_committees_at_slot,
    get_current_epoch_committee_count_per_slot,
    get_current_epoch_attestations,
    get_effective_balance,
    get_randao_mix,
    get_winning_root,
)
from eth2.beacon.types.crosslink_records import CrosslinkRecord
from eth2.beacon.types.states import BeaconState
from eth2.beacon.state_machines.configs import BeaconConfig


#
# Crosslinks
#
def process_crosslinks(state: BeaconState, config: BeaconConfig) -> BeaconState:
    """
    Implements 'per-epoch-processing.crosslinks' portion of Phase 0 spec:
    https://github.com/ethereum/eth2.0-specs/blob/master/specs/core/0_beacon-chain.md#crosslinks

    For the shard to be crosslinked during each slots of the past two epochs, find the shard block
    root with the most stake attestted to.
    If enough(>= 2/3 total stake) attesting stake, update the crosslink record of that shard.
    Return resulting ``state``
    """
    latest_crosslinks = state.latest_crosslinks
    current_epoch_attestations = get_current_epoch_attestations(state, config.EPOCH_LENGTH)
    # TODO: STUB, in spec it was
    # `for slot in range(state.slot - 2 * config.EPOCH_LENGTH, state.slot):``
    # waiting for ethereum/eth2.0-specs#492 to update the spec
    for slot in range(state.slot - 1 * config.EPOCH_LENGTH, state.slot):
        crosslink_committees_at_slot = get_crosslink_committees_at_slot(
            state,
            slot,
            config.EPOCH_LENGTH,
            config.TARGET_COMMITTEE_SIZE,
            config.SHARD_COUNT,
        )
        for crosslink_committee, shard in crosslink_committees_at_slot:
            # Filter out attestations not attesting to this shard so we don't need
            # to going over irrelevent attestations over and over again.
            to_this_shard_attestations = [
                a
                for a in current_epoch_attestations
                if a.data.shard == shard
            ]
            winning_root = get_winning_root(state, config, to_this_shard_attestations, shard)
            attesting_validators_indices = get_attesting_validator_indices(
                state,
                config.EPOCH_LENGTH,
                to_this_shard_attestations,
                config.TARGET_COMMITTEE_SIZE,
                config.SHARD_COUNT,
                shard,
                winning_root,
            )
            total_attesting_balance = sum(
                [
                    get_effective_balance(state.validator_balances, i, config.MAX_DEPOSIT)
                    for i in attesting_validators_indices
                ]
            )
            total_balance = sum(
                [
                    get_effective_balance(state.validator_balances, i, config.MAX_DEPOSIT)
                    for i in crosslink_committee
                ]
            )
            if 3 * total_attesting_balance >= 2 * total_balance:
                latest_crosslinks = update_tuple_item(
                    latest_crosslinks,
                    shard,
                    CrosslinkRecord(
                        slot=state.slot,
                        shard_block_root=winning_root,
                    )
                )
    state = state.copy(
        latest_crosslinks=latest_crosslinks,
    )
    return state


#
# Validator registry and shuffling seed data
#
def _check_if_update_validator_registry(state: BeaconState,
                                        config: BeaconConfig) -> Tuple[bool, int]:
    if state.finalized_slot <= state.validator_registry_update_slot:
        return False, 0

    num_shards_in_committees = get_current_epoch_committee_count_per_slot(
        state,
        shard_count=config.SHARD_COUNT,
        epoch_length=config.EPOCH_LENGTH,
        target_committee_size=config.TARGET_COMMITTEE_SIZE,
    ) * config.EPOCH_LENGTH

    # Get every shard in the current committees
    shards = set(
        (state.current_epoch_start_shard + i) % config.SHARD_COUNT
        for i in range(num_shards_in_committees)
    )
    for shard in shards:
        if state.latest_crosslinks[shard].slot <= state.validator_registry_update_slot:
            return False, 0

    return True, num_shards_in_committees


def update_validator_registry(state: BeaconState) -> BeaconState:
    # TODO
    return state


def _update_latest_index_roots(state: BeaconState,
                               config: BeaconConfig) -> BeaconState:
    """
    Return the BeaconState with updated `latest_index_roots`.
    """
    next_epoch = state.next_epoch(config.EPOCH_LENGTH)

    # TODO: chanege to hash_tree_root
    active_validator_indices = get_active_validator_indices(
        state.validator_registry,
        # TODO: change to `per-epoch` version
        state.slot,
    )
    index_root = hash_eth2(
        b''.join(
            [
                index.to_bytes(32, 'big')
                for index in active_validator_indices
            ]
        )
    )

    latest_index_roots = update_tuple_item(
        state.latest_index_roots,
        next_epoch % config.LATEST_INDEX_ROOTS_LENGTH,
        index_root,
    )

    return state.copy(
        latest_index_roots=latest_index_roots,
    )


def process_validator_registry(state: BeaconState,
                               config: BeaconConfig) -> BeaconState:
    state = state.copy(
        previous_epoch_calculation_slot=state.current_epoch_calculation_slot,
        previous_epoch_start_shard=state.current_epoch_start_shard,
        previous_epoch_seed=state.current_epoch_seed,
    )
    state = _update_latest_index_roots(state, config)

    need_to_update, num_shards_in_committees = _check_if_update_validator_registry(state, config)

    if need_to_update:
        state = update_validator_registry(state)

        # Update step-by-step since updated `state.current_epoch_calculation_slot`
        # is used to calculate other value). Follow the spec tightly now.
        state = state.copy(
            current_epoch_calculation_slot=state.slot,
        )
        state = state.copy(
            current_epoch_start_shard=(
                state.current_epoch_start_shard + num_shards_in_committees
            ) % config.SHARD_COUNT,
        )

        # The `helpers.generate_seed` function is only present to provide an entry point
        # for mocking this out in tests.
        current_epoch_seed = helpers.generate_seed(
            state=state,
            slot=state.current_epoch_calculation_slot,
            epoch_length=config.EPOCH_LENGTH,
            seed_lookahead=config.SEED_LOOKAHEAD,
            latest_index_roots_length=config.LATEST_INDEX_ROOTS_LENGTH,
            latest_randao_mixes_length=config.LATEST_RANDAO_MIXES_LENGTH,
        )
        state = state.copy(
            current_epoch_seed=current_epoch_seed,
        )
    else:
        epochs_since_last_registry_change = (
            state.slot - state.validator_registry_update_slot
        ) // config.EPOCH_LENGTH
        if is_power_of_two(epochs_since_last_registry_change):
            # Update step-by-step since updated `state.current_epoch_calculation_slot`
            # is used to calculate other value). Follow the spec tightly now.
            state = state.copy(
                current_epoch_calculation_slot=state.slot,
            )

            # The `helpers.generate_seed` function is only present to provide an entry point
            # for mocking this out in tests.
            current_epoch_seed = helpers.generate_seed(
                state=state,
                slot=state.current_epoch_calculation_slot,
                epoch_length=config.EPOCH_LENGTH,
                seed_lookahead=config.SEED_LOOKAHEAD,
                latest_index_roots_length=config.LATEST_INDEX_ROOTS_LENGTH,
                latest_randao_mixes_length=config.LATEST_RANDAO_MIXES_LENGTH,
            )
            state = state.copy(
                current_epoch_seed=current_epoch_seed,
            )
        else:
            pass

    return state


#
# Final updates
#
def process_final_updates(state: BeaconState,
                          config: BeaconConfig) -> BeaconState:
    epoch = state.slot // config.EPOCH_LENGTH
    current_index = (epoch + 1) % config.LATEST_PENALIZED_EXIT_LENGTH
    previous_index = epoch % config.LATEST_PENALIZED_EXIT_LENGTH

    state = state.copy(
        latest_penalized_balances=update_tuple_item(
            state.latest_penalized_balances,
            current_index,
            state.latest_penalized_balances[previous_index],
        ),
    )

    epoch_start = state.slot - config.EPOCH_LENGTH
    latest_attestations = tuple(
        filter(
            lambda attestation: attestation.data.slot >= epoch_start,
            state.latest_attestations
        )
    )
    state = state.copy(
        latest_attestations=latest_attestations,
    )

    return state
