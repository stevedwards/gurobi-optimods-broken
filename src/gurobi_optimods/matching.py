import collections
import logging
from typing import Optional, overload

import numpy as np
import pandas as pd
import scipy.sparse as sp

import gurobipy as gp
from gurobipy import GRB

try:
    import networkx as nx
except ImportError:
    nx = None

from gurobi_optimods.network_util import solve_min_cost_flow
from gurobi_optimods.utils import optimod

logger = logging.getLogger(__name__)


@overload
def maximum_bipartite_matching(
    graph: sp.spmatrix,
    nodes1: np.ndarray,
    nodes2: np.ndarray,
    silent: bool = False,
    logfile: Optional[str] = None,
) -> sp.spmatrix:
    ...


@overload
def maximum_bipartite_matching(
    graph: pd.DataFrame,
    nodes1: str,
    nodes2: str,
    silent: bool = False,
    logfile: Optional[str] = None,
) -> pd.DataFrame:
    ...


if nx is not None:

    @overload
    def maximum_bipartite_matching(
        graph: nx.Graph,
        nodes1: np.ndarray,
        nodes2: np.ndarray,
        silent: bool = False,
        logfile: Optional[str] = None,
    ) -> nx.Graph:
        ...


@optimod()
def maximum_bipartite_matching(graph, nodes1, nodes2, *, create_env):
    """Solve a maximum cardinality bipartite matching problem on the
    given graph.

    :param graph: A graph, specified either as a scipy.sparse adjacency matrix, networkx
        graph, or pandas dataframe
    :type graph: :class:`sp.sparray|nx.Graph|pd.DataFrame`
    :param nodes1: Nodes in the first bipartite set. If ``graph`` is a pandas dataframe,
        nodes1 must be a column name. Otherwise, it is a numpy array of nodes in the first
        bipartite set.
    :type nodes1: :class:`np.array|str`
    :param nodes2: Nodes in the second bipartite set. If ``graph`` is a pandas dataframe,
        nodes2 must be a column name. Otherwise, it is a numpy array of nodes in the second
        bipartite set.
    :type nodes2: :class:`np.array|str`
    :param silent: silent=True suppresses all console output (defaults to False)
    :type silent: bool
    :param logfile: Write all mod output to the given file path (defaults to None: no log)
    :type logfile: str
    :return: A subgraph of the original graph specifying the maximum matching
    :rtype: :class:`sp.sparray|nx.Graph|pd.DataFrame`
    """
    if isinstance(graph, sp.spmatrix):
        return _maximum_bipartite_matching_scipy(graph, nodes1, nodes2, create_env)
    elif isinstance(graph, pd.DataFrame):
        return _maximum_bipartite_matching_pandas(graph, nodes1, nodes2, create_env)
    elif nx is not None and isinstance(graph, nx.Graph):
        return _maximum_bipartite_matching_networkx(graph, nodes1, nodes2, create_env)
    else:
        raise ValueError(f"Unknown graph type: {type(graph)}")


def _maximum_bipartite_matching_pandas(frame, n1_column, n2_column, create_env):
    # Turn categorical labels into disjoint sets in 0..N
    row = frame[n1_column].astype("category").cat.codes.to_numpy()
    col = frame[n2_column].astype("category").cat.codes.to_numpy() + row.max() + 1
    degree = col.max() + 1

    # Construct sparse matrix and solve
    data = np.ones(row.shape)
    adjacency = sp.coo_array((data, (row, col)), shape=(degree, degree))
    nodes1 = np.unique(row)
    nodes2 = np.unique(col)
    matching = _maximum_bipartite_matching_scipy(adjacency, nodes1, nodes2, create_env)

    # Join the original matrix to get the matching subset
    adj = sp.triu(matching)
    original = frame.assign(_n1_codes=row, _n2_codes=col)
    selected = pd.DataFrame({"n1": adj.row, "n2": adj.col})
    result = pd.merge(
        original,
        selected,
        left_on=["_n1_codes", "_n2_codes"],
        right_on=["n1", "n2"],
        suffixes=("", "_joined"),
    )
    # TODO gurobipy-pandas would definitely be cleaner!
    return result[frame.columns]


def _maximum_bipartite_matching_networkx(graph, nodes1, nodes2, create_env):
    adjacency = nx.convert_matrix.to_scipy_sparse_array(graph)
    matching = _maximum_bipartite_matching_scipy(adjacency, nodes1, nodes2, create_env)
    return nx.convert_matrix.from_scipy_sparse_array(matching)


def _maximum_bipartite_matching_scipy(adjacency, nodes1, nodes2, create_env):
    logger.info(
        f"Solving maximum matching n1={nodes1.shape[0]} "
        f"n2={nodes2.shape[0]} |E|={adjacency.data.shape[0]}"
    )

    # Add a source and sink node for max flow formulation
    # Assume G is symmetric (or upper triangular)
    G = sp.triu(adjacency.tocoo())
    G_nodes = adjacency.shape[0]
    source, sink = G_nodes, G_nodes + 1

    # Build network:
    #   source -> nodes1 (complete)
    #   nodes1 -> nodes2 (adjacency)
    #   nodes2 -> sink (complete)
    #   sink -> source
    from_arc = np.concatenate([np.repeat(source, nodes1.shape), G.row, nodes2, [sink]])
    to_arc = np.concatenate([nodes1, G.col, np.repeat(sink, nodes2.shape), [source]])
    capacity = np.ones(from_arc.shape, dtype=float)
    capacity[-1] = GRB.INFINITY
    cost = np.zeros(from_arc.shape, dtype=float)
    cost[-1] = -1.0
    balance = np.zeros(G_nodes + 2)
    logger.info(
        "Maximum matching formulated as min-cost flow with "
        f"{balance.shape[0]} nodes and {from_arc.shape[0]} arcs"
    )

    # Solve min-cost flow problem
    with create_env() as env:
        _, flows = solve_min_cost_flow(env, from_arc, to_arc, capacity, cost, balance)

    # Choose the arcs corresponding to the original graph with non-zero
    # flow. Note that the last var is the sink->source connection (drop it).
    select = (flows > 0.5) & (from_arc != source) & (to_arc != sink)
    from_arc_result = from_arc[select][:-1]
    to_arc_result = to_arc[select][:-1]

    logger.info(f"Done: max bipartite matching has {from_arc_result.shape[0]} edges")

    # Return undirected, unweighted adjacency matrix
    arg = (np.ones(from_arc_result.shape), (from_arc_result, to_arc_result))
    matching = sp.coo_matrix(arg, dtype=float, shape=G.shape)
    return matching + matching.T


@optimod()
def maximum_weighted_matching(G, *, create_env):
    """Return a subgraph which is the maximum weighted matching of G.

    :param G: Adjacency matrix of a unweighted graph.
    :type G: :class:`sp.sparray`
    :return: Adjacency matrix of the maximum weighted matching subgraph
    :rtype: :class:`sp.sparray`
    """

    logger.info(
        f"Solving weighted matching model with {G.shape[0]} nodes and "
        f"{int(G.nnz/2)} edges"
    )

    with create_env() as env, gp.Model(env=env) as m:
        G = G.tocoo()
        edges = list(zip(G.row, G.col))
        x = m.addVars(edges, name="x", vtype=GRB.BINARY)

        clashes = collections.defaultdict(set)
        for edge in edges:
            clashes[edge[0]].add(edge)
            clashes[edge[1]].add(edge)

        for edgepair in clashes.values():
            m.addConstr(gp.quicksum(x[edge] for edge in edgepair) <= 1)

        weights = dict(zip(edges, G.data))
        m.setObjective(x.prod(weights), sense=GRB.MAXIMIZE)
        m.optimize()

        row, col, data = zip(*[(i, j, v.Obj) for (i, j), v in x.items() if v.X > 0.5])

        logger.info(f"Max weighted matching has {len(data)} edges")

        return sp.coo_array((data, (row, col)), shape=G.shape)
