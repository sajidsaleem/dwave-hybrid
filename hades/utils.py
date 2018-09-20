from __future__ import division

import random

import six
import dimod
import numpy

from dnx import canonical_chimera_labeling


def bqm_reduced_to(bqm, variables, fixed_values, keep_offset=True):
    """Reduce a binary quadratic model by fixing values of some variables.

    The function is optimized for ``len(variables) ~ len(bqm)``, that is,
    for small numbers of fixed variables.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        variables (list/set);
            Subset of variables to keep in the reduced BQM.
        fixed_values (dict/list): Mapping of variable labels to values or a list when labels
            are sequential integers. Must include all variables not specified in `variables`.
        keep_offset (bool, optional, default=True): If false, set the reduced binary quadratic
            model’s offset to zero; otherwise, uses the caluclated energy offset.

    Returns:
            :class:`dimod.BinaryQuadraticModel`: A reduced BQM.

    Examples:
        This example reduces a 3-variable BQM to two variables.

        >>> import dimod           # Create a binary quadratic model
        >>> bqm = dimod.BinaryQuadraticModel({}, {('a', 'b'): -1, ('b', 'c'): -1, ('c', 'a'): -1}, 0, 'BINARY')
        >>> fixed_values = {'a': 1, 'b': 1, 'c': 0}
        >>> bqm_reduced_to(bqm, ['a', 'b'], fixed_values)
        BinaryQuadraticModel({'a': 0.0, 'b': 0.0}, {('a', 'b'): -1}, 0.0, Vartype.BINARY)

    """

    # fix complement of ``variables```
    fixed = set(bqm.variables).difference(variables)
    subbqm = bqm.copy()
    for v in fixed:
        subbqm.fix_variable(v, fixed_values[v])

    if not keep_offset:
        subbqm.remove_offset()

    return subbqm


def bqm_induced_by(bqm, variables, fixed_values):
    """Induce a binary quadratic model by fixing values of boundary variables.

    The function is optimized for ``len(variables) << len(bqm)``, that is, for fixing
    the majority of variables.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        variables (list/set);
            Subset of variables to keep in the reduced BQM, typically a subgraph.
        fixed_values (dict/list):
            Mapping of variable labels to values or a list when labels
            are sequential integers. Values are required only for boundary variables,
            that is, for variables with interactions with `variables` (having edges
            with non-zero quadratic biases connected to the subgraph).

    Returns:
        :class:`dimod.BinaryQuadraticModel`: A BQM induced by fixing values of
        those variables adjacent to its subset of variables and setting the energy offset
        to zero.

    Examples:
        This example induces a 2-variable BQM from a 6-variable path graph---the subset
        of nodes 2 and 3 of nodes 0 to 5---by fixing values of boundary variables 1 and 4.

        >>> import dimod           # Create a binary quadratic model from a path graph
        >>> import networkx as nx
        >>> bqm = dimod.BinaryQuadraticModel({},
        ...             {edge: edge[0] for edge in set(nx.path_graph(6).edges)}, 0, 'BINARY')
        >>> fixed_values = {1: 3, 4: 3}
        >>> bqm_induced_by(bqm, [2, 3], fixed_values)
        BinaryQuadraticModel({2: 3.0, 3: 9.0}, {(2, 3): 2.0}, 0.0, Vartype.BINARY)

    """

    variables = set(variables)

    # create empty BQM and copy in a subgraph induced by `variables`
    subbqm = dimod.BinaryQuadraticModel({}, {}, 0.0, bqm.vartype)

    for u in variables:
        bias = bqm.linear[u]
        for v, j in bqm.adj[u].items():
            if v in variables:
                subbqm.add_interaction(u, v, j / 2.0)
            else:
                bias += j * fixed_values[v]
        subbqm.add_variable(u, bias)

    # no point in having offset since we're fixing only variables on boundary
    subbqm.remove_offset()

    return subbqm


def bqm_edges_between_variables(bqm, variables):
    """Return edges connecting specified variables of a binary quadratic model.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        variables (list/set):
            Subset of variables in the BQM.

    Returns:
        list: All edges connecting `variables` as tuples plus the variables themselves
        as tuples (v, v).

    Examples:
        This example returns connecting edges between 3 nodes of a BQM based on a 4-variable
        path graph.

        >>> import dimod           # Create a binary quadratic model
        >>> bqm = dimod.BinaryQuadraticModel({}, {(0, 1): 1, (1, 2): 1, (2, 3): 1}, 0, 'BINARY')
        >>> bqm_edges_between_variables(bqm, {0, 1, 3})
        [(0, 1), (0, 0), (1, 1), (3, 3)]

    """
    variables = set(variables)
    edges = [(start, end) for (start, end), coupling in bqm.quadratic.items() if start in variables and end in variables]
    edges.extend((v, v) for v in bqm.linear if v in variables)
    return edges


def flip_energy_gains_naive(bqm, sample):
    """Return `list[(energy_gain, flip_index)]` in descending order
    for flipping qubit with flip_index in sample.

    Note: Grossly inefficient! Use `flip_energy_gains_iterative` which traverses
    variables, updating energy delta based on previous var value and neighbors.
    """

    if bqm.vartype is dimod.BINARY:
        flip = lambda val: 1 - val
    elif bqm.vartype is dimod.SPIN:
        flip = lambda val: -val
    else:
        raise ValueError("vartype not supported")

    base = bqm.energy(sample)
    sample = sample_as_list(sample)
    energy_gains = [(bqm.energy(sample[:i] + [flip(val)] + sample[i+1:]) - base, i) for i, val in enumerate(sample)]
    energy_gains.sort(reverse=True)
    return energy_gains

    # Performance comparison to flip_energy_gains_iterative (bqm size ~ 2k, random sample)::
    #   >>> %timeit flip_energy_gains_naive(bqm, sample)
    #   3.35 s ± 37.5 ms per loop (mean ± std. dev. of 7 runs, 1 loop each)
    #   >>> %timeit flip_energy_gains_iterative(bqm, sample)
    #   3.52 ms ± 20.4 µs per loop (mean ± std. dev. of 7 runs, 100 loops each)
    #  Three orders of magnitude faster.

def flip_energy_gains_iterative(bqm, sample):
    """Order variable flips by descending contribution to energy changes in a BQM.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        sample (list/dict):
            Sample values as returned by dimod samplers (0 or 1 values for dimod.BINARY
            and -1 or +1 for dimod.SPIN)

    Returns:
        list: Energy changes in descending order, in the format of tuples
            (energy_gain, variable), for flipping the given sample value
            for each variable.

    Examples:
        This example returns connecting edges between 3 nodes of a BQM based on a 4-variable
        path graph.

        >>> import dimod           # Create a binary quadratic model
        >>> bqm = dimod.BinaryQuadraticModel({},
        ...             {('a', 'b'): 0, ('b', 'c'): 1, ('c', 'd'): 2}, 0, 'SPIN')
        >>> flip_energy_gains_iterative(bqm, {'a': -1, 'b': 1, 'c': 1, 'd': -1})
        [(4.0, 'd'), (2.0, 'c'), (0.0, 'a'), (-2.0, 'b')]

    """

    if bqm.vartype is dimod.BINARY:
        # val is 0, flips to 1 => delta +1
        # val is 1, flips to 0 => delta -1
        delta = lambda val: 1 - 2 * val
    elif bqm.vartype is dimod.SPIN:
        # val is -1, flips to +1 => delta +2
        # val is +1, flips to -1 => delta -2
        delta = lambda val: -2 * val
    else:
        raise ValueError("vartype not supported")

    energy_gains = []
    sample = sample_as_dict(sample)
    # list comprehension speeds-up the iterative approach by
    # only 2%. Using standard loop for readablity
    for idx, val in sample.items():
        contrib = bqm.linear[idx] + sum(w * sample[neigh] for neigh, w in bqm.adj[idx].items())
        energy_gains.append((contrib * delta(val), idx))

    energy_gains.sort(reverse=True)
    return energy_gains


flip_energy_gains = flip_energy_gains_iterative


def select_localsearch_adversaries(bqm, sample, max_n=None, min_gain=None):
    """Find variable flips that contribute high energy changes to a BQM.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        sample (list/dict):
            Sample values as returned by dimod samplers (0 or 1 values for dimod.BINARY
            and -1 or +1 for dimod.SPIN)
        max_n (int, optional, default=None):
            Maximum contributing variables to return. By default, returns any variable
            for which flipping its sample value results in an energy gain of `min_gain`.
        min_gain (float, optional, default=None):
            Minimum required energy increase from flipping a sample value to return
            its corresponding variable.

    Returns:
        list: Up to `max_n` variables for which flipping the corresponding sample value
        increases the BQM energy by at least `min_gain`.

    Examples:
        This example returns 2 variables (out of up to 3 allowed) for which flipping
        sample values changes BQM energy by 1 or more. The BQM has energy gains
        of  0, -2, 2, 4 for variables a, b, c, d respectively for the given sample.

        >>> import dimod           # Create a binary quadratic model
        >>> bqm = dimod.BinaryQuadraticModel({},
        ...             {('a', 'b'): 0, ('b', 'c'): 1, ('c', 'd'): 2}, 0, 'SPIN')
        >>> select_localsearch_adversaries(bqm, {'a': -1, 'b': 1, 'c': 1, 'd': -1},
        ...                                max_n=3, min_gain=1)
        ['d', 'c']

    """
    var_gains = flip_energy_gains(bqm, sample)

    if max_n is None:
        max_n = len(sample)
    if min_gain is None:
        variables = [var for _, var in var_gains]
    else:
        variables = [var for en, var in var_gains if en >= min_gain]

    return variables[:max_n]


def select_random_subgraph(bqm, n):
    """Select randomly `n` variables of the specified binary quadratic model.

    Args:
        bqm (:class:`dimod.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        n (int):
            Number of requested variables. Must be between 0 and `len(bqm)`.

    Returns:
        list: `n` variables selected randomly from the BQM.

    Examples:
        This example returns 2 variables of a 4-variable BQM.

        >>> import dimod           # Create a binary quadratic model
        >>> bqm = dimod.BinaryQuadraticModel({},
        ...             {('a', 'b'): 0, ('b', 'c'): 1, ('c', 'd'): 2}, 0, 'BINARY')
        >>> select_random_subgraph(bqm, 2)      # doctest: +SKIP
        ['d', 'b']

    """
    return random.sample(bqm.linear.keys(), n)


def chimera_tiles(bqm, m, n, t):
    """Map a binary quadratic model to a set of Chimera tiles.

    A Chimera lattice is an m-by-n grid of Chimera tiles, where each tile is a bipartite graph
    with shores of size t.

    Args:
        bqm (:obj:`.BinaryQuadraticModel`):
            Binary quadratic model (BQM).
        m (int): Rows.
        n (int): Columns.
        t (int): Size of shore.

    Returns:
        dict: Map as a dict where keys are tile coordinates (row, column, aisle) and values
        are partial embeddings of part of the BQM to a Chimera tile. Embeddings are
        those that would be generated by dwave_networkx's chimera_graph() function.

    Examples:
        This example maps a 1-by-2 Chimera-derived BQM to 2 side-by-side tiles.

        >>> import dwave_networkx as dnx
        >>> import dimod
        >>> G = dnx.chimera_graph(1, 2)     # Create a Chimera-based BQM
        >>> bqm = dimod.BinaryQuadraticModel({}, {edge: edge[0] for edge in G.edges}, 0, 'BINARY')
        >>> chimera_tiles(bqm, 1, 1, 4)     # doctest: +SKIP
        {(0, 0, 0): {0: [0], 1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6], 7: [7]},
         (0, 1, 0): {8: [0], 9: [1], 10: [2], 11: [3], 12: [4], 13: [5], 14: [6], 15: [7]}}

    """
    try:
        chimera_indices = canonical_chimera_labeling(bqm)
    except AssertionError:
        raise ValueError("non-Chimera structured problem")

    max_m = max(i for i, _, _, _ in chimera_indices.values()) + 1
    max_n = max(j for _, j, _, _ in chimera_indices.values()) + 1
    max_t = max(k for _, _, _, k in chimera_indices.values()) + 1

    tile_rows = -(max_m // -m)  # ceiling division
    tile_columns = -(max_n // -n)
    tile_shore_length = -(max_t // -t)

    tiles = {(row, col, aisle): {}
             for row in range(tile_rows)
             for col in range(tile_columns)
             for aisle in range(tile_shore_length)}

    for v, (si, sj, u, sk) in chimera_indices.items():
        row = si % tile_rows  # which tile
        i = si // tile_rows  # which row within the tile

        col = sj % tile_columns
        j = sj // tile_columns

        aisle = sk % tile_shore_length
        k = sk // tile_shore_length

        tiles[(row, col, aisle)][v] = [((n*i + j)*2 + u)*t + k]

    return tiles


def updated_sample(sample, replacements):
    """Update a copy of a sample with replacement values.

    Args:
        sample (list/dict): Sample values as returned by dimod samplers to be copied.
        replacements (list/dict): Sample values to replace in the copied `sample`.

    Returns:
        list/dict: Copy of `sample` overwritten by specified values.

    Examples:
        >>> sample = {'a': 1, 'b': 1}
        >>> updated_sample(sample, {'b': 2})
        {'a': 1, 'b': 2}

    """
    result = sample_as_dict(sample).copy()
    for k, v in sample_as_dict(replacements).items():
        result[k] = v
    return result


def sample_as_list(sample):
    """Return sample object in dict format.

    Args:
        sample (list/dict/dimod.SampleView): Sample object formatted as a list,
        Numpy array, dict, or as returned by dimod samplers. Variable labeling
        must be numerical.

    Returns:
        list: Copy of `sample` formatted as a list.

    Examples:
        >>> sample = {0: 1, 1: 1}
        >>> sample_as_list(sample)
        [1, 1]

    """
    if isinstance(sample, list):
        return sample
    if isinstance(sample, numpy.ndarray):
        return sample.tolist()
    indices = sorted(dict(sample).keys())
    if len(indices) > 0 and indices[-1] - indices[0] + 1 != len(indices):
        raise ValueError("incomplete sample dict")
    return [sample[k] for k in indices]


def sample_as_dict(sample):
    """Convert list-like ``sample`` (list/dict/dimod.SampleView),
    ``list: var``, to ``map: idx -> var``.
    """
    if isinstance(sample, dict):
        return sample
    if isinstance(sample, (list, numpy.ndarray)):
        sample = enumerate(sample)
    return dict(sample)


@dimod.decorators.vartype_argument('vartype')
def random_sample_seq(size, vartype):
    """Return random sample of `size` in length, with values from `vartype`."""
    values = list(vartype.value)
    return {i: random.choice(values) for i in range(size)}


def random_sample(bqm):
    values = list(bqm.vartype.value)
    return {i: random.choice(values) for i in bqm.variables}


def min_sample(bqm):
    value = min(bqm.vartype.value)
    return {i: value for i in bqm.variables}


def max_sample(bqm):
    value = max(bqm.vartype.value)
    return {i: value for i in bqm.variables}
