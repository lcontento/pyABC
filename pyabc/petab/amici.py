import logging
from collections.abc import Sequence, Mapping
from typing import Callable, Union, Dict
import copy

import pyabc
from .base import PetabImporter

logger = logging.getLogger(__name__)

try:
    import petab
except ImportError:
    logger.error("Install petab (see https://github.com/icb-dcm/petab) to use "
                 "the petab functionality.")

try:
    import amici
    import amici.petab_import
    from amici.petab_objective import simulate_petab, LLH, RDATAS
except ImportError:
    logger.error("Install amici (see https://github.com/icb-dcm/amici) to use "
                 "the amici functionality.")


class AmiciPetabImporter(PetabImporter):
    """
    Import a PEtab model using AMICI to simulate it as a deterministic ODE.

    Parameters
    ----------

    petab_problem:
        A PEtab problem containing all information on the parameter estimation
        problem.
    free_parameters:
        Whether to estimate free parameters (column ESTIMATE=1 in the
        parameters table).
    fixed_parameters:
        Whether to estimate fixed parameters (column ESTIMATE=0 in the
        parameters table).
    amici_model:
        A corresponding compiled AMICI model that allows simulating data for
        parameters. If not provided, one is created using
        `amici.petab_import.import_petab_problem`.
    amici_solver:
        An AMICI solver to simulate the model. If not provided, one is created
        using `amici_model.getSolver()`.
    """

    def __init__(
            self,
            petab_problem: petab.Problem,
            amici_model: amici.Model = None,
            amici_solver: amici.Solver = None,
            free_parameters: bool = True,
            fixed_parameters: bool = False):
        super().__init__(
            petab_problem=petab_problem,
            free_parameters=free_parameters,
            fixed_parameters=fixed_parameters)

        if amici_model is None:
            amici_model = amici.petab_import.import_petab_problem(
                petab_problem)
        self.amici_model = amici_model

        if amici_solver is None:
            amici_solver = self.amici_model.getSolver()
        self.amici_solver = amici_solver

    def create_model(
            self,
            return_simulations: bool = False,
            return_rdatas: bool = False,
    ) -> Callable[[Union[Sequence, Mapping]], Mapping]:
        """Create model.

        Note that since AMICI uses deterministic ODE simulations,
        it is usually not necessary to store simulations, as these can
        be reproduced from the parameters.

        Parameters
        ----------
        return_simulations:
            Whether to return the simulations also (large, can be stored
            in database).
        return_rdatas:
            Whether to return the full `List[amici.ExpData]` objects (large,
            cannot be stored in database).

        Returns
        -------
        model:
            The model function, taking parameters and returning simulations.
            The model returns already the likelihood value.
        """
        # parameter ids to consider
        x_ids = self.petab_problem.get_x_ids(
            free=self.free_parameters, fixed=self.fixed_parameters)

        # fixed paramters
        x_fixed_ids = self.petab_problem.get_x_ids(
            free=not self.free_parameters, fixed=not self.fixed_parameters)
        x_fixed_vals = self.petab_problem.get_x_nominal(
            scaled=True,
            free=not self.free_parameters, fixed=not self.fixed_parameters)

        # no gradients for pyabc
        self.amici_solver.setSensitivityOrder(0)

        return AmiciPyABCModel(
            self.petab_problem, self.amici_model, self.amici_solver,
            x_ids, x_fixed_ids, x_fixed_vals,
            return_simulations, return_rdatas
        )

    def create_kernel(
        self,
    ) -> pyabc.StochasticKernel:
        """
        Create acceptance kernel.

        Returns
        -------
        kernel:
            A pyabc distribution encoding the kernel function.
        """
        def kernel_fun(x, x_0, t, par) -> float:
            """The kernel function."""
            # the kernel value is computed by amici already
            return x['llh']

        # create a kernel from function, returning log-scaled values
        kernel = pyabc.distance.SimpleFunctionKernel(
            kernel_fun, ret_scale=pyabc.distance.SCALE_LOG)

        return kernel


class AmiciPyABCModel:
    def __init__(
        self, petab_problem, amici_model, amici_solver,
        x_ids, x_fixed_ids, x_fixed_vals,
        return_simulations, return_rdatas):
        self.petab_problem = petab_problem
        self.amici_model = amici_model
        self.amici_solver = amici_solver
        self.x_ids = x_ids
        self.x_fixed_ids = x_fixed_ids
        self.x_fixed_vals = x_fixed_vals
        self.return_simulations = return_simulations
        self.return_rdatas = return_rdatas

    def __call__(self, par: Union[Sequence, Mapping]) -> Mapping:
        # copy since we add fixed parameters
        par = copy.deepcopy(par)

        # convenience to allow calling model not only with dicts
        if not isinstance(par, Mapping):
            par = {key: val for key, val in zip(self.x_ids, par)}

        # add fixed parameters
        for key, val in zip(self.x_fixed_ids, self.x_fixed_vals):
            par[key] = val

        # simulate model
        sim = simulate_petab(
            petab_problem=self.petab_problem,
            amici_model=self.amici_model,
            solver=self.amici_solver,
            problem_parameters=par,
            scaled_parameters=True)

        # return values of interest
        ret = {'llh': sim[LLH]}
        if self.return_simulations:
            for i_rdata, rdata in enumerate(sim[RDATAS]):
                ret[f'y_{i_rdata}'] = rdata['y']
        if self.return_rdatas:
            ret[RDATAS] = sim[RDATAS]

        return ret

    def __getstate__(self) -> Dict:
        state = {}
        for key in set(self.__dict__.keys()) - {'amici_model', 'amici_solver'}:
            state[key] = self.__dict__[key]

        _fd, _file = tempfile.mkstemp()
        try:
            # write amici solver settings to file
            try:
                amici.writeSolverSettingsToHDF5(
                    self.amici_solver, _file)
            except AttributeError as e:
                e.args += ("Pickling the AmiciObjective requires an AMICI "
                           "installation with HDF5 support.",)
                raise
            # read in byte stream
            with open(_fd, 'rb', closefd=False) as f:
                state['amici_solver_settings'] = f.read()
        finally:
            # close file descriptor and remove temporary file
            os.close(_fd)
            os.remove(_file)

        return state

    def __setstate__(self, state: Dict):
        self.__dict__.update(state)

        model = amici.petab_import.import_petab_problem(self.petab_problem)
        solver = model.getSolver()

        _fd, _file = tempfile.mkstemp()
        try:
            # write solver settings to temporary file
            with open(_fd, 'wb', closefd=False) as f:
                f.write(state['amici_solver_settings'])
            # read in solver settings
            try:
                amici.readSolverSettingsFromHDF5(_file, solver)
            except AttributeError as err:
                if not err.args:
                    err.args = ('',)
                err.args += ("Unpickling an AmiciObjective requires an AMICI "
                             "installation with HDF5 support.",)
                raise
        finally:
            # close file descriptor and remove temporary file
            os.close(_fd)
            os.remove(_file)

        self.amici_model = model
        self.amici_solver = solver
