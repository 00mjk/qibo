# -*- coding: utf-8 -*-
# @authors: S. Efthymiou
import copy
import numpy as np
import tensorflow as tf
import joblib
from qibo.config import DTYPES
from qibo.base import gates
from qibo.tensorflow import callbacks, circuit, measurements
from qibo.tensorflow import custom_operators as op
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
InitStateType = Union[np.ndarray, tf.Tensor]
OutputType = Union[tf.Tensor, measurements.CircuitResult]


class DeviceQueues:
    """Data structure that holds gate queues for each accelerator device.

    For a distributed simulation we have to swap global qubits multiple times.
    For each global qubit configuration a several gates can be applied to the
    state forming a gate group. Once all gates in the group are applied the
    global qubits are swapped and we proceed to the next gate group.

    ``DeviceQueues`` holds the following data that define the gate groups and
    corresponding global qubits:
    * ``nqubits``: Total number of qubits in the circuit.
    * ``ndevices``: Number of logical accelerator devices.
    * ``nglobal``: Number of global qubits (= log2(ndevices)).
    * ``nlocal``: Number of local qubits (= nqubits - nglobal).
    * ``gate_module``: Gate module used by the circuit. This is used to create
        new SWAP gates when needed.

    * ``global_qubits_list``: Sorted list with the ids of global qubits.
    * ``global_qubits_set``: Same as ``global_qubits_list`` but in a set to
        allow O(1) search.
    * ``local_qubits``: Sorted list with the ids of local qubits.
    * ``global_qubits_reduced``: Map from global qubit ids to their reduced
        value. The reduced value is the effective id in a hypothetical circuit
        that does not contain the local qubits.
    * ``local_qubits_reduced``: Map from local qubit ids to their reduced
        value. The reduced value is the effective id in a hypothetical circuit
        that does not contain the global qubits.

    * ``device_to_ids``: Dictionary that maps device (str) to list of piece indices.
        When a device is used multiple times then it is responsible for updating
        multiple state pieces. The list of indices specifies which pieces the device
        will update.
    * ``ids_to_device``: Inverse dictionary of ``device_to_ids``.

    * ``queues``: Nested list of shape ``(ngroups, ndevices, group size)``.
        For example ``queues[2][1]`` gives the gate queue of the second gate
        group to be run in the first device.
        If ``gate[i]`` is an empty list it means that this the i-th group
        consists of a special gate to be run on ``memory_device``.
    * ``special_queue``: List with special gates than run on the full state vector
        on ``memory_device``. Special gates have no target qubits and can be
        ``CallbackGate``, ``Flatten`` or SWAPs between local and global qubits.
    """

    def __init__(self, circuit: "TensorflowDistributedCircuit",
                 global_qubits: Set[int]):
        self.nqubits = circuit.nqubits
        self.ndevices = circuit.ndevices
        self.nglobal = circuit.nglobal
        self.nlocal = circuit.nlocal
        self.gate_module = circuit.gate_module

        self.queues = []
        self.special_queue = []

        self.global_qubits_set = global_qubits
        self.global_qubits_list = sorted(global_qubits)
        self.local_qubits = [q for q in range(self.nqubits)
                             if q not in self.global_qubits_set]

        self.global_qubits_reduced = {q: self.global_qubits_list.index(q)
                                      for q in self.global_qubits_list}
        self.local_qubits_reduced = {q: q - self.reduction_number(q)
                                     for q in self.local_qubits}

        # List that holds the global-local SWAP pairs so that we can reset them
        # in the end
        self.swaps_list = []

        self.device_to_ids = {d: v for d, v in self._ids(circuit.calc_devices)}
        self.ids_to_device = self.ndevices * [None]
        for device, ids in self.device_to_ids.items():
            for i in ids:
                self.ids_to_device[i] = device

    def _ids(self, calc_devices: Dict[str, int]) -> Tuple[str, List[int]]:
        """Generator of device piece indices."""
        start = 0
        for device, n in calc_devices.items():
            stop = start + n
            yield device, list(range(start, stop))
            start = stop

    def reduction_number(self, q: int) -> int:
        """Calculates the effective id in a circuit without the global qubits."""
        for i, gq in enumerate(self.global_qubits_list):
            if gq > q:
                return i
        return i + 1

    def _create_reduced_gate(self, gate: gates.Gate) -> gates.Gate:
        """Creates a copy of a gate for specific device application.

        Target and control qubits are modified according to the local qubits of
        the circuit when this gate will be applied.

        Args:
            gate: The :class:`qibo.base.gates.Gate` object of the gate to copy.

        Returns:
            A :class:`qibo.base.gates.Gate` object with the proper target and
            control qubit indices for device-specific application.
        """
        calc_gate = copy.copy(gate)
        # Recompute the target/control indices considering only local qubits.
        new_target_qubits = tuple(q - self.reduction_number(q)
                                  for q in calc_gate.target_qubits)
        new_control_qubits = tuple(q - self.reduction_number(q)
                                   for q in calc_gate.control_qubits
                                   if q not in self.global_qubits_set)
        calc_gate.set_targets_and_controls(new_target_qubits, new_control_qubits)
        calc_gate.original_gate = gate
        return calc_gate

    @staticmethod
    def count(queue: List[gates.Gate], nqubits: int) -> np.ndarray:
        """Counts how many gates target each qubit.

        Args:
            queue: List of gates.
            nqubits: Number of total qubits in the circuit.

        Returns:
            Array of integers with shape (nqubits,) with the number of gates
            for each qubit id.
        """
        counter = np.zeros(nqubits, dtype=np.int32)
        for gate in queue:
            for qubit in gate.target_qubits:
                counter[qubit] += 1
        return counter

    def _transform(self, queue: List[gates.Gate],
                   remaining_queue: List[gates.Gate],
                   counter: np.ndarray) -> List[gates.Gate]:
        """Helper recursive method for ``transform``."""
        new_remaining_queue = []
        for gate in remaining_queue:
            if gate.is_special_gate:
                gate.swap_reset = list(self.swaps_list)

            global_targets = set(gate.target_qubits) & self.global_qubits_set
            accept = isinstance(gate, gates.SWAP) and len(global_targets) == 1
            accept = accept or not global_targets
            for skipped_gate in new_remaining_queue:
                accept = accept and skipped_gate.commutes(gate)
                if not accept:
                    break
            if accept:
                queue.append(gate)
                for q in gate.target_qubits:
                    counter[q] -= 1
            else:
                new_remaining_queue.append(gate)

        if not new_remaining_queue:
            return queue

        # Find which qubits to swap
        gate = new_remaining_queue[0]
        target_set = set(gate.target_qubits)
        global_targets = target_set & self.global_qubits_set
        if isinstance(gate, gates.SWAP): # special case of swap on two global qubits
            assert len(global_targets) == 2
            global_targets.remove(target_set.pop())

        available_swaps = (q for q in counter.argsort()
                           if q not in self.global_qubits_set | target_set)
        qubit_map = {}
        for q in global_targets:
            qs = next(available_swaps)
            # Update qubit map that holds the swaps
            qubit_map[q] = qs
            qubit_map[qs] = q
            # Keep SWAPs in memory to reset them in the end
            self.swaps_list.append((min(q, qs), max(q, qs)))
            # Add ``SWAP`` gate in ``queue``.
            queue.append(self.gate_module.SWAP(q, qs))
            #  Modify ``counter`` to take into account the swaps
            counter[q], counter[qs] = counter[qs], counter[q]

        # Modify gates to take into account the swaps
        for gate in new_remaining_queue:
            new_target_qubits = tuple(qubit_map[q] if q in qubit_map else q
                                       for q in gate.target_qubits)
            new_control_qubits = tuple(qubit_map[q] if q in qubit_map else q
                                        for q in gate.control_qubits)
            gate.set_targets_and_controls(new_target_qubits, new_control_qubits)

        return self._transform(queue, new_remaining_queue, counter)

    def transform(self, queue: List[gates.Gate],
                  counter: Optional[np.ndarray] = None) -> List[gates.Gate]:
        """Transforms gate queue to be compatible with distributed simulation.

        Adds SWAP gates between global and local qubits so that no gates are
        applied to global qubits.

        Args:
            queue (list): Original gate queue.
            counter (np.ndarray): Counter of how many gates target each qubit.
                If ``None`` this is calculated using the ``count`` method.

        Returns:
            List of gates that have the same effect as the original queue but
            are compatible with distributed run (do not have global qubits as
            targets).
        """
        if counter is None:
            counter = self.count(queue, self.nqubits)
        new_queue = self._transform([], queue, counter)
        new_queue.extend((self.gate_module.SWAP(*p)
                          for p in reversed(self.swaps_list)))
        return new_queue

    def create(self, queue: List[gates.Gate]):
        """Creates the queues for each accelerator device.

        Args:
            queue (list): List of gates compatible with distributed run.
            If the original ``queue`` contains gates that target global qubits
            then ``transform` should be used to obtain a compatible queue.
        """
        for gate in queue:
            if not gate.target_qubits: # special gate
                gate.nqubits = self.nqubits
                self.special_queue.append(gate)
                self.queues.append([])

            elif set(gate.target_qubits) & self.global_qubits_set: # global swap gate
                global_qubits = set(gate.target_qubits) & self.global_qubits_set
                if not isinstance(gate, gates.SWAP):
                    raise ValueError("Only SWAP gates are supported for "
                                     "global qubits.")
                if len(global_qubits) > 1:
                    raise ValueError("SWAPs between global qubits are not allowed.")

                global_qubit = global_qubits.pop()
                local_qubit = gate.target_qubits[0]
                if local_qubit == global_qubit:
                    local_qubit = gate.target_qubits[1]

                self.special_queue.append((global_qubit, local_qubit))
                self.queues.append([])

            else:
                if not self.queues or not self.queues[-1]:
                    self.queues.append([[] for _ in range(self.ndevices)])

                for device, ids in self.device_to_ids.items():
                    calc_gate = self._create_reduced_gate(gate)
                    # Gate matrix should be constructed in the calculation
                    # device otherwise device parallelization will break
                    with tf.device(device):
                        calc_gate.nqubits = self.nlocal
                    for i in ids:
                        flag = True
                        # If there are control qubits that are global then
                        # the gate should not be applied by all devices
                        for control in (set(gate.control_qubits) &
                                        self.global_qubits_set):
                            ic = self.global_qubits_list.index(control)
                            ic = self.nglobal - ic - 1
                            flag = bool((i // (2 ** ic)) % 2)
                            if not flag:
                                break
                        if flag:
                            self.queues[-1][i].append(calc_gate)


class TensorflowDistributedCircuit(circuit.TensorflowCircuit):
    """Distributed implementation of :class:`qibo.base.circuit.BaseCircuit` in Tensorflow.

    Uses multiple `accelerator` devices (GPUs) for applying gates to the state vector.
    The full state vector is saved in the given `memory device` (usually the CPU)
    during the simulation. A gate is applied by splitting the state to pieces
    and copying each piece to an accelerator device that is used to perform the
    matrix multiplication. An `accelerator` device can be used more than once
    resulting to logical devices that are more than the physical accelerators in
    the system.

    Distributed circuits currently do not support native tensorflow gates,
    compilation and callbacks.

    Example:
        ::

            from qibo.models import Circuit
            # The system has two GPUs and we would like to use each GPU twice
            # resulting to four total logical accelerators
            accelerators = {'/GPU:0': 2, '/GPU:1': 2}
            # Define a circuit on 32 qubits to be run in the above GPUs keeping
            # the full state vector in the CPU memory.
            c = Circuit(32, accelerators, memory_device="/CPU:0")

    Args:
        nqubits (int): Total number of qubits in the circuit.
        accelerators (dict): Dictionary that maps device names to the number of
            times each device will be used.
            The total number of logical devices must be a power of 2.
        memory_device (str): Name of the device where the full state will be
            saved (usually the CPU).
    """

    def __init__(self,
                 nqubits: int,
                 accelerators: Dict[str, int],
                 memory_device: str = "/CPU:0"):
        super(TensorflowDistributedCircuit, self).__init__(nqubits)
        self._init_kwargs.update({"accelerators": accelerators,
                                  "memory_device": memory_device})
        self.ndevices = sum(accelerators.values())
        self.nglobal = float(np.log2(self.ndevices))
        if not (self.nglobal.is_integer() and self.nglobal > 0):
            raise ValueError("Number of calculation devices should be a power "
                             "of 2 but is {}.".format(self.ndevices))
        self.nglobal = int(self.nglobal)
        self.nlocal = self.nqubits - self.nglobal

        self.memory_device = memory_device
        self.calc_devices = accelerators

        self.queues = None
        self.pieces = None
        self._construct_shapes()

    def _construct_shapes(self):
        """Useful shapes for the simulation."""
        dtype = DTYPES.get('DTYPEINT')
        n = self.nqubits - self.nglobal
        self.device_shape = tf.cast((self.ndevices, 2 ** n), dtype=dtype)
        self.full_shape = tf.cast((2 ** self.nqubits,), dtype=dtype)
        self.tensor_shape = self.nqubits * (2,)

        self.local_full_shape = tf.cast((2 ** n,), dtype=dtype)
        self.local_tensor_shape = n * (2,)

    def _set_nqubits(self, gate):
        # Do not set ``gate.nqubits`` during gate addition because this will
        # be set by the ``set_gates`` method once all gates are known.
        if gate._nqubits is not None:
            raise ValueError("Attempting to add gate with preset number of "
                             "qubits in distributed circuit.")

    @property
    def global_qubits(self) -> List[int]:
        """Returns the global qubits IDs in a sorted list.

        The global qubits are used to split the state to multiple pieces.
        Gates that have global qubits as their target qubits cannot be applied
        using the accelerators. In order to apply such gates we have to swap
        the target global qubit with a different (local) qubit.
        """
        if self.queues is None:
            raise ValueError("Cannot access global qubits before being set.")
        return self.queues.global_qubits_list

    @global_qubits.setter
    def global_qubits(self, x: Sequence[int]):
        """Sets the current global qubits.

        At the same time the ``transpose_order`` and ``reverse_transpose_order``
        lists are set. These lists are used in order to transpose the state pieces
        when we want to swap global qubits.
        """
        global_qubit_set = set(x)
        if len(global_qubit_set) != self.nglobal:
            raise ValueError("Invalid number of global qubits {} for using {} "
                             "calculation devices.".format(len(x), self.ndevices))

        self.queues = DeviceQueues(self, global_qubit_set)

        self.transpose_order = (self.queues.global_qubits_list +
                                self.queues.local_qubits)
        self.reverse_transpose_order = self.nqubits * [0]
        for i, v in enumerate(self.transpose_order):
            self.reverse_transpose_order[v] = i

    def copy(self, deep: bool = True) -> "TensorflowDistributedCircuit":
        if not deep:
            raise ValueError("Non-deep copy is not allowed for distributed "
                             "circuits because they modify gate objects.")
        return super(TensorflowDistributedCircuit, self).copy(deep)

    def with_noise(self, noise_map, measurement_noise=None):
        raise NotImplementedError("Distributed circuit does not support "
                                  "density matrices yet.")

    def _add(self, gate: gates.Gate):
        """Adds a gate in the circuit (inherited from :class:`qibo.base.circuit.BaseCircuit`).

        Also checks that there are sufficient qubits to use as global.
        """
        if isinstance(gate, gates.VariationalLayer):
            gate._prepare()
        elif (self.nqubits - len(gate.target_qubits) < self.nglobal and
              not isinstance(gate, gates.M)):
            raise ValueError("Insufficient qubits to use for global in "
                             "distributed circuit.")
        super(TensorflowDistributedCircuit, self)._add(gate)

    def set_gates(self):
        """Prepares gates for device-specific gate execution.

        Each gate has to be recreated in the device that will be executed to
        allow parallel execution. The global qubit lists and gate groups are
        also specified here.
        A gate group is identified by looping through the circuit's gate queue
        and adding gates in the group until the number of global becomes ``nglobal``.
        Once this happens no more gates can be added in the group. In order to
        apply new gates some global qubits have to be swapped to global and a
        new gate group will be defined for the new global qubit configuration.

        The final global qubit lists and gate queues that are used for execution
        are storred in ``self.queues`` which is a ``DeviceQueues`` object.
        """
        if not self.queue:
            raise RuntimeError("No gates available to set for distributed run.")

        # Count how many gates target each qubit to identify global qubits
        counter = DeviceQueues.count(self.queue, self.nqubits)
        if self.queues is None:
            self.global_qubits = counter.argsort()[:self.nglobal]

        transformed_queue = self.queues.transform(self.queue, counter)
        self.queues.create(transformed_queue)

    def compile(self):
        raise RuntimeError("Cannot compile circuit that uses custom operators.")

    def _device_execute(self, state: tf.Tensor, gates: List["TensorflowGate"]) -> tf.Tensor:
        for gate in gates:
            state = gate(state)
        return state

    # Old casting on CPU after runs finish. Not used because it leads to
    # GPU memory errors
    #def _cast_results(self, results: List[List[tf.Tensor]]):
    #    i = 0
    #    for result in results:
    #        for s in result:
    #            self.pieces[i].assign(s)
    #            i += 1

    def _joblib_execute(self, queues: List[List["TensorflowGate"]]):
        """Executes gates in ``accelerators`` in parallel.

        Args:
            queues: List that holds the gates to be applied by each accelerator.
                Has shape ``(ndevices, ngates_i)`` where ``ngates_i`` is the
                number of gates to be applied by accelerator ``i``.
        """
        def _device_job(ids, device):
            for i in ids:
                with tf.device(device):
                    state = self._device_execute(self.pieces[i], queues[i])
                    self.pieces[i].assign(state)
                    del(state)

        pool = joblib.Parallel(n_jobs=len(self.calc_devices),
                               prefer="threads")
        pool(joblib.delayed(_device_job)(ids, device)
             for device, ids in self.queues.device_to_ids.items())

    def _swap(self, global_qubit: int, local_qubit: int):
        m = self.queues.global_qubits_reduced[global_qubit]
        m = self.nglobal - m - 1
        t = 1 << m
        for g in range(self.ndevices // 2):
            i = ((g >> m) << (m + 1)) + (g & (t - 1))
            local_eff = self.queues.local_qubits_reduced[local_qubit]
            with tf.device(self.memory_device):
                op.swap_pieces(self.pieces[i], self.pieces[i + t],
                               local_eff, self.nlocal)

    def _revert_swaps(self, swap_pairs: List[Tuple[int, int]]):
        for q1, q2 in swap_pairs:
            if q1 not in self.queues.global_qubits_set:
                q1, q2 = q2, q1
            self._swap(q1, q2)

    def _special_gate_execute(self, gate: Union["TensorflowGate", Tuple[int, int]]):
        """Executes special gates (``Flatten`` or ``CallbackGate``) on ``memory_device``.

        These gates require the full state vector (cannot be executed in the state pieces).
        """
        if isinstance(gate, tuple): # SWAP global
            self._swap(*gate)
        else: # ``Flatten`` or callback
            with tf.device(self.memory_device):
                # Reverse all global SWAPs that happened so far
                self._revert_swaps(reversed(gate.swap_reset))
                state = self._merge()
                if isinstance(gate, gates.CallbackGate):
                    gate(state)
                else:
                    state = gate(state)
                    self._split(state)
                # Redo all global SWAPs that happened so far
                self._revert_swaps(gate.swap_reset)

    def _execute(self, initial_state: Optional[InitStateType] = None,
                 nshots: Optional[int] = None) -> OutputType:
        """Performs ``circuit.execute``."""
        if self.queues is None or not self.queues.queues:
            self.set_gates()
        self._cast_initial_state(initial_state)

        special_gates = iter(self.queues.special_queue)
        for i, queues in enumerate(self.queues.queues):
            if queues:  # standard gate
                self._joblib_execute(queues)
            else: # special gate
                self._special_gate_execute(next(special_gates))
        for gate in special_gates:
            self._special_gate_execute(next(special_gates))

        # NOTE: The final state will use the transpose op if ``global_qubits``
        # are not [0, 1, ..., nglobal] or [nlocal, nlocal+1, ..., nqubits].
        state = self.final_state
        if self.measurement_gate is None or nshots is None:
            return state

        with tf.device(self.memory_device):
            samples = self.measurement_gate(state, nshots, samples_only=True,
                                            is_density_matrix=self.using_density_matrix)
            self.measurement_gate_result = measurements.GateResult(
                self.measurement_gate.qubits, state, decimal_samples=samples)
            result = measurements.CircuitResult(
                self.measurement_tuples, self.measurement_gate_result)
        return result

    def execute(self, initial_state: Optional[InitStateType] = None,
                nshots: Optional[int] = None) -> OutputType:
        """Same as the ``execute`` method of :class:`qibo.tensorflow.circuit.TensorflowCircuit`."""
        oom_error = tf.python.framework.errors_impl.ResourceExhaustedError
        try:
            return self._execute(initial_state=initial_state, nshots=nshots)
        except oom_error:
            raise RuntimeError("State does not fit in memory during distributed "
                               "execution. Please create a new circuit with "
                               "different device configuration and try again.")

    @property
    def final_state(self) -> tf.Tensor:
        """Final state as a Tensorflow tensor of shape ``(2 ** nqubits,)``.

        The circuit has to be executed at least once before accessing this
        property, otherwise a ``ValueError`` is raised. If the circuit is
        executed more than once, only the last final state is returned.
        """
        if self.pieces is None:
            raise RuntimeError("Cannot access the state tensor before being set.")
        return self._merge()

    def _default_global_qubits(self) -> List[int]:
        """Returns a list with the last qubits to cast them as global."""
        return list(range(self.nglobal))

    def _default_initial_piece(self) -> tf.Tensor:
        """Returns the 0th piece for the |000...0> state."""
        zeros = tf.zeros(2 ** (self.nqubits - self.nglobal), dtype=DTYPES.get('DTYPECPX'))
        return op.initial_state(zeros)

    def _create_pieces(self):
        """Creates the state pieces as ``tf.Variable``s stored in the ``memory_device``."""
        n = 2 ** (self.nqubits - self.nglobal)
        with tf.device(self.memory_device):
            self.pieces = [tf.Variable(tf.zeros(n, dtype=DTYPES.get('DTYPECPX')))
                           for _ in range(self.ndevices)]

    def _default_initial_state(self) -> tf.Tensor:
        """Assigns the default |000...0> state to the state pieces."""
        self._create_pieces()
        with tf.device(self.memory_device):
            self.pieces[0].assign(self._default_initial_piece())

    def _cast_initial_state(self, initial_state: Optional[Union[np.ndarray, tf.Tensor]] = None) -> tf.Tensor:
        """Checks and casts initial state given by user."""
        if self.queues is None:
            self.global_qubits = self._default_global_qubits()

        if initial_state is None:
            return self._default_initial_state()

        state = super(TensorflowDistributedCircuit, self)._cast_initial_state(initial_state)
        if self.pieces is None:
            self._create_pieces()
        self._split(state)

    def _split(self, state: tf.Tensor):
        """Splits a given state vector and assigns it to the ``tf.Variable`` pieces.

        Args:
            state (tf.Tensor): Full state vector as a tensor of shape ``(2 ** nqubits)``.
        """
        with tf.device(self.memory_device):
            state = tf.reshape(state, self.device_shape)
            pieces = [state[i] for i in range(self.ndevices)]
            new_state = tf.zeros(self.device_shape, dtype=DTYPES.get('DTYPECPX'))
            new_state = op.transpose_state(pieces, new_state, self.nqubits, self.transpose_order)
            for i in range(self.ndevices):
                self.pieces[i].assign(new_state[i])

    def _merge(self) -> tf.Tensor:
        """Merges the current ``tf.Variable`` pieces to a full state vector.

        Returns:
            state (tf.Tensor): Full state vector as a tensor of shape ``(2 ** nqubits)``.
        """
        if self.global_qubits == list(range(self.nglobal)):
            with tf.device(self.memory_device):
                state = tf.concat([x[tf.newaxis] for x in self.pieces], axis=0)
                state = tf.reshape(state, self.full_shape)
        elif self.global_qubits == list(range(self.nlocal, self.nqubits)):
            with tf.device(self.memory_device):
                state = tf.concat([x[:, tf.newaxis] for x in self.pieces], axis=1)
                state = tf.reshape(state, self.full_shape)
        else: # fall back to the transpose op
            with tf.device(self.memory_device):
                state = tf.zeros(self.full_shape, dtype=DTYPES.get('DTYPECPX'))
                state = op.transpose_state(self.pieces, state, self.nqubits,
                                           self.reverse_transpose_order)
        return state
