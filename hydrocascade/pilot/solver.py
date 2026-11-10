"""Audited HiGHS mixed-integer solve without changing the shared LP implementation."""

from time import perf_counter
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import vstack

from ..lp import LP, OptimizationError
from ..model import finite


class MILP(LP):
    def __init__(self):
        super().__init__()
        self.binaries = []

    def binary(self, key, cost=0):
        col = self.var(key, cost=cost, upper=1)
        self.binaries.append(col)
        return col

    def solve(self, time_limit_seconds=60, relative_gap=1e-6):
        finite(time_limit_seconds, "time_limit_seconds", 1e-6)
        finite(relative_gap, "relative_gap", 0)
        if relative_gap > 0.01:
            raise ValueError("relative_gap must be <= 0.01")
        start = perf_counter()
        matrix = vstack((self.matrix(self.eq), self.matrix(self.ub)), format="csc")
        rhs = np.r_[self.eq_rhs, self.ub_rhs]
        lower = np.r_[self.eq_rhs, np.full(len(self.ub), -np.inf)]
        integrality = np.zeros(len(self.keys), dtype=int)
        integrality[self.binaries] = 1
        lows, highs = zip(*self.bounds)
        highs = [np.inf if h is None else h for h in highs]
        result = milp(
            self.costs,
            integrality=integrality,
            bounds=Bounds(lows, highs),
            constraints=LinearConstraint(matrix, lower, rhs),
            options={"time_limit": time_limit_seconds, "mip_rel_gap": relative_gap},
        )
        if not result.success or result.x is None:
            raise OptimizationError(f"MILP status {result.status}: {result.message}; no accepted schedule")
        activity = matrix @ result.x
        row_error = float(max(np.max(activity - rhs, initial=0), np.max(lower - activity, initial=0)))
        bound_error = float(
            max(np.max(np.asarray(lows) - result.x, initial=0), np.max(result.x - highs, initial=0))
        )
        integer_error = float(
            np.max(np.abs(result.x[self.binaries] - np.round(result.x[self.binaries])), initial=0)
        )
        if max(row_error, bound_error, integer_error) > 1e-5:
            raise OptimizationError("MILP solution failed independent row/bound/integrality checks")
        result.audit = dict(
            solver="SciPy / HiGHS MILP",
            status=int(result.status),
            message=result.message,
            solve_seconds=perf_counter() - start,
            objective=float(result.fun),
            mip_gap=float(result.mip_gap),
            mip_dual_bound=float(result.mip_dual_bound),
            mip_nodes=int(result.mip_node_count),
            variables=len(self.keys),
            binary_variables=len(self.binaries),
            equality_constraints=len(self.eq),
            inequality_constraints=len(self.ub),
            max_row_violation=row_error,
            max_bound_violation=bound_error,
            max_integrality_violation=integer_error,
        )
        return result
