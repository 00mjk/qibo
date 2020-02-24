"""
Testing Quantum Fourier Transform (QFT) circuit.
"""
import numpy as np
import pytest
from qibo import gates, models


def qft_matrix(dimension: int, inverse: bool = False) -> np.ndarray:
    """Creates exact QFT matrix.

    Args:
        dimension: Dimension d of the matrix. The matrix will be d x d.
        inverse: Whether to construct matrix for the inverse QFT.

    Return:
        QFT transformation matrix as a numpy array with shape (d, d).
    """
    exponent = np.outer(np.arange(dimension), np.arange(dimension))
    sign = 1 - 2 * int(inverse)
    return np.exp(sign * 2 * np.pi * 1j * exponent / dimension)


def exact_qft(x: np.ndarray, inverse: bool = False) -> np.ndarray:
    """Performs exact QFT to a given state vector."""
    dim = len(x)
    return qft_matrix(dim, inverse).dot(x) / np.sqrt(dim)


def test_qft_sanity():
    """Check QFT circuit size and depth."""
    c = models.QFTCircuit(4)
    assert c.size == 4
    assert c.depth == 12


@pytest.mark.parametrize("nqubits", [4, 5])
def test_qft_transformation(nqubits):
    """Check QFT transformation for |00...0>."""
    c = models.QFTCircuit(nqubits)
    final_state = c.execute().numpy()

    initial_state = np.zeros_like(final_state)
    initial_state[0] = 1.0
    exact_state = exact_qft(initial_state)

    np.testing.assert_allclose(final_state, exact_state)


def random_state(nqubits):
    x = np.random.random(2**nqubits) + 1j * np.random.random(2**nqubits)
    return x / np.sqrt((np.abs(x)**2).sum())


@pytest.mark.parametrize("nqubits", [4, 5, 11, 12])
def test_qft_transformation_random(nqubits):
    """Check QFT transformation for random initial state."""
    initial_state = random_state(nqubits)
    exact_state = exact_qft(initial_state)

    c_init = models.Circuit(nqubits)
    c_init.add(gates.Flatten(initial_state))
    c = c_init + models.QFTCircuit(nqubits)
    final_state = c.execute().numpy()

    np.testing.assert_allclose(final_state, exact_state)


@pytest.mark.parametrize("nqubits", [4, 5, 11, 12])
def test_qftgates_transformation_random(nqubits):
    """Check QFT transformation of `QFTGates` for random initial state."""
    initial_state = random_state(nqubits)
    exact_state = exact_qft(initial_state)

    initial_state = initial_state.reshape(nqubits * (2,))
    final_state = models.QFTGates(initial_state)

    np.testing.assert_allclose(final_state, exact_state)