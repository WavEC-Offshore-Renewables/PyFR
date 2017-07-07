# -*- coding: utf-8 -*-

from collections import defaultdict
import itertools as it
import re

from pyfr.inifile import Inifile
from pyfr.integrators.dual.base import BaseDualIntegrator
from pyfr.util import memoize, proxylist


class MultiP(BaseDualIntegrator):
    def __init__(self, backend, systemcls, rallocs, mesh, initsoln, cfg):
        sect = 'solver-dual-time-integrator-multip'

        # Get the solver order
        order = cfg.getint('solver', 'order')

        # Get the multigrid cycle
        self.cycles = list(cfg.getliteral(sect, 'cycle'))
        self.nlvls = len(self.cycles) // 2 + 1
        self.levels = (list(range(self.nlvls - 1)) +
                       list(range(self.nlvls - 1, -1, -1)))
        self.level = 0

        if self.nlvls > order:
            raise ValueError('The number of multigrid levels cannot exceed '
                             'the solution order')

        # Multigrid pseudo-time steps
        dtau = cfg.getfloat('solver-time-integrator', 'pseudo-dt')
        dtaufact = cfg.getfloat(sect, 'dt-fact', 1.0)
        self.dtaus = [dtau*dtaufact**i for i in range(self.nlvls)]

        # Generate multiple cfgs for the multigrid systems
        self.mgcfg = [Inifile(cfg.tostr()) for i in range(self.nlvls)]
        for deg in range(1, self.nlvls + 1):
            self.mgcfg[deg - 1].set('solver', 'order', order - (deg - 1))
            for sec in cfg.sections():
                m = re.match(r'solver-(.*)-mg-p{0}'.format(deg), sec)
                if m:
                    self.mgcfg[order - deg].rename_section(
                        m.group(0), 'solver-' + m.group(1)
                    )

        super().__init__(backend, systemcls, rallocs, mesh, initsoln, cfg)

        # Delete remaining elements maps from multigrid systems
        for sys in self._mgsystem[1:]:
            del sys.ele_map

    @property
    def _idxcurr(self):
        return self._mgidxcurr[self.level]

    @_idxcurr.setter
    def _idxcurr(self, y):
        self._mgidxcurr[self.level] = y

    @property
    def _regs(self):
        return self._mgregs[self.level]

    @_regs.setter
    def _regs(self, y):
        self._mgidxcurr[self.level] = y

    def _init_proj_mats(self):
        self.projmats = defaultdict(proxylist)
        cmat = lambda m: self.backend.const_matrix(m, tags={'align'})

        for l in range(self.nlvls - 1):
            for etype in self.system.ele_types:
                b1 = self._mgsystem[l].ele_map[etype].basis.ubasis
                b2 = self._mgsystem[l + 1].ele_map[etype].basis.ubasis
                self.projmats[l, l + 1].append(cmat(b1.proj_to(b2)))
                self.projmats[l + 1, l].append(cmat(b2.proj_to(b1)))

    @memoize
    def mgproject(self, l1, l2):
        inbanks = self._mgsystem[l1].eles_scal_upts_inb
        outbanks = self._mgsystem[l2].eles_scal_upts_inb

        return proxylist(
            self.backend.kernel('mul', pm, inb, out=outb)
            for pm, inb, outb in zip(self.projmats[l1, l2], inbanks, outbanks)
        )

    def restrict(self, l1, l2, dt):
        l1idxcurr, l2idxcurr = self._mgidxcurr[l1], self._mgidxcurr[l2]
        l1sys, l2sys = self._mgsystem[l1], self._mgsystem[l2]
        mg0, mg1, mg2 = self._mg_regidx

        add, rhs = self._add, self._rhs_with_dts

        # mg1 = R = -∇·f - dQ/dt
        rhs(self.tcurr, l1idxcurr, mg1, c=1/dt, passmg=True)

        # mg1 = d = r - R
        add(-1, mg1, 0 if l1 == 0 else 1, mg0)

        # Restrict Q
        l1sys.eles_scal_upts_inb.active = l1idxcurr
        l2sys.eles_scal_upts_inb.active = l2idxcurr
        self._queue % self.mgproject(l1, l2)()

        # Need to store the non-smoothed solution Q^ns for the correction
        # mg2 = Q^ns
        self.level = l2
        add(0, mg2, 1, l2idxcurr)

        # Restrict d and store to m1
        l1sys.eles_scal_upts_inb.active = mg1
        l2sys.eles_scal_upts_inb.active = mg1
        self._queue % self.mgproject(l1, l2)()

        # mg0 = R = -∇·f - dQ/dt
        rhs(self.tcurr, l2idxcurr, mg0, c=1/dt, passmg=True)

        # Compute the target residual r
        # mg0 = r = R + d
        add(1, mg0, 1, mg1)

        # Restrict the dt source terms
        for srcidx in self._source_regidx:
            l1sys.eles_scal_upts_inb.active = srcidx
            l2sys.eles_scal_upts_inb.active = srcidx
            self._queue % self.mgproject(l1, l2)()

    def prolongate(self, l1, l2):
        l1idxcurr, l2idxcurr = self._mgidxcurr[l1], self._mgidxcurr[l2]
        l1sys, l2sys = self._mgsystem[l1], self._mgsystem[l2]
        mg0, mg1, mg2 = self._mg_regidx

        # Correction with respect to the non-smoothed value from down-cycle
        # mg1 = Delta = Q^s - Q^ns
        self._add(0, mg1, 1, l1idxcurr, -1, mg2)

        # Prolongate the correction and store to mg1
        l1sys.eles_scal_upts_inb.active = mg1
        l2sys.eles_scal_upts_inb.active = mg1
        self._queue % self.mgproject(l1, l2)()

        # Add the correction to the end quantity at l2
        # Q^m+1  = Q^s + Delta
        self.level = l2
        self._add(1, l2idxcurr, 1, mg1)

    def _rhs_with_dts(self, t, uin, fout, c=1, passmg=False):
        # Compute -∇·f
        self.system.rhs(t, uin, fout)

        # Coefficients for the dual-time source term
        svals = [c*sc for sc in self._dual_time_source]

        # Source addition -∇·f - dQ/dt
        axnpby = self._get_axnpby_kerns(len(svals) + 1, level=self.level,
                                        subdims=self._subdims)
        self._prepare_reg_banks(fout, self._idxcurr, *self._source_regidx)
        self._queue % axnpby(1, *svals)

        # Multigrid r addition
        if self.level != 0 and not passmg:
            axnpby = self._get_axnpby_kerns(2, level=self.level)
            self._prepare_reg_banks(fout, self._mg_regidx[0])
            self._queue % axnpby(1, -1)

    def _add(self, *args):
        # Get a suitable set of axnpby kernels
        axnpby = self._get_axnpby_kerns(len(args) // 2, level=self.level)

        # Bank indices are in odd-numbered arguments
        self._prepare_reg_banks(*args[1::2])

        # Bind and run the axnpby kernels
        self._queue % axnpby(*args[::2])

    @property
    def _mg_regidx(self):
        return self._regidx[-3:]

    @property
    def system(self):
        return self._mgsystem[self.level]

    def _init_reg_banks(self):
        # Three additional banks are required for multigrid
        self._mgregs, self._regidx = [], list(range(self.nreg + 3))
        self._mgidxcurr = [0]*self.nlvls

        # Create a proxylist of matrix-banks for each storage register
        for l in range(self.nlvls):
            self._mgregs.append(
                [proxylist(self.backend.matrix_bank(em, i)
                           for em in self._mgsystem[l].ele_banks)
                 for i in self._regidx]
            )

    def _init_system(self, systemcls, *args):
        self._mgsystem = [systemcls(*args, nreg=self.nreg + 3,
                                    cfg=self.mgcfg[i])
                          for i in range(self.nlvls)]

        # Initialise the restriction and prolongation matrices
        self._init_proj_mats()

    def advance_to(self, t):
        if t < self.tcurr:
            raise ValueError('Advance time is in the past')

        # Multigrid levels and step counts
        levels, cycles = self.levels, self.cycles

        while self.tcurr < t:
            dt = max(min(t - self.tcurr, self._dt), self.dtmin)

            for i in range(self._maxniters):
                # V-cycle
                for l, m, n in it.zip_longest(levels, levels[1:], cycles):
                    self.level = l

                    dtau = max(min(t - self.tcurr, self.dtaus[l]),
                               self._dtaumin)

                    for k in range(n):
                        self._idxcurr, idxprev = self.step(self.tcurr, dt, dtau)

                    if m and l < m:
                        self.restrict(l, m, dt)
                    elif m and l > m:
                        self.prolongate(l, m)


                if i >= self._minniters - 1:
                    # Subtract the current and previous solution
                    self._add(-1, idxprev, 1, self._idxcurr)

                    # Compute the normalised residual
                    resid = tuple(self._resid(self.dtaus[0], idxprev))
                else:
                    resid = None

                # Increment the step count
                self.npseudosteps += 1
                self.pseudostepinfo.append((self.npseudosteps, i + 1, resid))

                # Check for convergence
                if resid and max(resid) < self._pseudo_residtol:
                    break

            # Update the dual-time stepping banks
            self.finalise_step(self._idxcurr)

            # We are not adaptive, so accept every step
            self._accept_step(dt, self._idxcurr)
