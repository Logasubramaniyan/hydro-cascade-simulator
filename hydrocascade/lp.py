"""Small sparse linear-program builder shared by hydro and market models."""

from time import perf_counter
import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix


class OptimizationError(RuntimeError):
    pass


class LP:
    def __init__(self):
        self.keys, self.costs, self.bounds = [], [], []
        self.eq, self.ub, self.eq_rhs, self.ub_rhs = [], [], [], []

    def var(self, key, cost=0.0, lower=0.0, upper=None):
        i = len(self.keys)
        self.keys.append(key)
        self.costs.append(cost)
        self.bounds.append((lower, upper))
        return i

    def equality(self, coefficients, rhs):
        self.eq.append(coefficients)
        self.eq_rhs.append(rhs)
        return len(self.eq) - 1

    def inequality(self, coefficients, rhs):
        self.ub.append(coefficients)
        self.ub_rhs.append(rhs)

    def matrix(self, rows):
        data, ri, ci = [], [], []
        for r, row in enumerate(rows):
            for c, value in row.items():
                if value:
                    data.append(value)
                    ri.append(r)
                    ci.append(c)
        return coo_matrix((data, (ri, ci)), shape=(len(rows), len(self.keys))).tocsr()

    def solve(self):
        start = perf_counter()
        aeq = self.matrix(self.eq) if self.eq else None
        aub = self.matrix(self.ub) if self.ub else None
        result = linprog(
            self.costs,
            A_eq=aeq,
            b_eq=self.eq_rhs or None,
            A_ub=aub,
            b_ub=self.ub_rhs or None,
            bounds=self.bounds,
            method="highs",
        )
        if not result.success:
            raise OptimizationError(f"HiGHS status {result.status}: {result.message}")
        x = result.x
        eq_error = np.max(np.abs(aeq @ x - self.eq_rhs), initial=0) if self.eq else 0.0
        ub_error = np.max(aub @ x - self.ub_rhs, initial=0) if self.ub else 0.0
        bound_error = max(
            [0.0] + [max(lo - v, 0 if hi is None else v - hi) for v, (lo, hi) in zip(x, self.bounds)]
        )
        result.audit = dict(
            status=int(result.status),
            message=result.message,
            solver="SciPy / HiGHS LP",
            objective=float(result.fun),
            solve_seconds=perf_counter() - start,
            variables=len(self.keys),
            equality_constraints=len(self.eq),
            inequality_constraints=len(self.ub),
            max_equality_residual=float(eq_error),
            max_inequality_violation=float(max(ub_error, 0)),
            max_bound_violation=float(bound_error),
        )
        if max(eq_error, ub_error, bound_error) > 1e-5:
            raise OptimizationError("Solver solution failed independent residual checks")
        return result
