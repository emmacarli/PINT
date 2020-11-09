from __future__ import absolute_import, division, print_function

import abc
import astropy.units as u
import numpy as np
from scipy.linalg import LinAlgError
from astropy import log
import collections

from pint.phase import Phase
from pint.utils import weighted_mean

__all__ = ["Residuals", "WidebandDMResiduals", "residual_map"]


class Residuals:
    """Class to compute residuals between TOAs and a TimingModel

    Parameters
    ----------
    subtract_mean : bool
        Controls whether mean will be subtracted from the residuals
    use_weighted_mean : bool
        Controls whether mean compution is weighted (by errors) or not.
    track_mode : "nearest", "use_pulse_numbers"
        Controls how pulse numbers are assigned. The default "nearest" assigns each TOA to the nearest integer pulse.
        "use_pulse_numbers" uses the pulse_number column of the TOAs table to assign pulse numbers. This mode is
        selected automatically if the model has parameter TRACK == "-2".
    """

    def __new__(
        cls,
        toas=None,
        model=None,
        residual_type="toa",
        unit=u.s,
        subtract_mean=True,
        use_weighted_mean=True,
        track_mode="nearest",
        scaled_by_F0=True,
    ):
        if cls is Residuals:
            try:
                cls = residual_map[residual_type.lower()]
            except KeyError:
                raise ValueError(
                    "'{}' is not a PINT supported residual. Currently "
                    "support data type are {}".format(
                        residual_type, list(residual_map.keys())
                    )
                )

        return super().__new__(cls)

    def __init__(
        self,
        toas=None,
        model=None,
        residual_type="toa",
        unit=u.s,
        subtract_mean=True,
        use_weighted_mean=True,
        track_mode="nearest",
        scaled_by_F0=True,
    ):
        self.toas = toas
        self.model = model
        self.residual_type = residual_type
        self.subtract_mean = subtract_mean
        self.use_weighted_mean = use_weighted_mean
        self.track_mode = track_mode
        if getattr(self.model, "TRACK").value == "-2":
            self.track_mode = "use_pulse_numbers"
        if toas is not None and model is not None:
            self.phase_resids = self.calc_phase_resids()
            self.time_resids = self.calc_time_resids()
            self.dof = self.get_dof()
        else:
            self.phase_resids = None
            self.time_resids = None
        # delay chi-squared computation until needed to avoid infinite recursion
        # also it's expensive
        # only relevant if there are correlated errors
        self._chi2 = None
        self.noise_resids = {}
        self.scaled_by_F0 = scaled_by_F0
        # We should be carefully for the other type of residuals
        self.unit = unit

    @property
    def resids(self):
        if self.scaled_by_F0:
            return self.time_resids
        else:
            if self.phase_resids is None:
                self.phase_resids = self.calc_phase_resids()
            return self.phase_resids

    @property
    def data_error(self):
        return self.toas.get_errors()

    @property
    def chi2_reduced(self):
        return self.chi2 / self.dof

    @property
    def chi2(self):
        """Compute chi-squared as needed and cache the result"""
        if self._chi2 is None:
            self._chi2 = self.calc_chi2()
        assert self._chi2 is not None
        return self._chi2

    @property
    def resids_value(self):
        """ Get pure value of the residuals use the given base unit.
        """
        if not self.scaled_by_F0:
            return self.resids.to_value(self.unit / u.s)
        else:
            return self.resids.to_value(self.unit)

    def rms_weighted(self):
        """Compute weighted RMS of the residals in time."""
        if np.any(self.toas.get_errors() == 0):
            raise ValueError(
                "Some TOA errors are zero - cannot calculate weighted RMS of residuals"
            )
        w = 1.0 / (self.toas.get_errors().to(u.s) ** 2)

        wmean, werr, wsdev = weighted_mean(self.time_resids, w, sdev=True)
        return wsdev.to(u.us)

    def calc_phase_resids(self):
        """Return timing model residuals in pulse phase."""

        # Read any delta_pulse_numbers that are in the TOAs table.
        # These are for PHASE statements, -padd flags, as well as user-inserted phase jumps
        # Check for the column, and if not there then create it as zeros
        try:
            delta_pulse_numbers = Phase(self.toas.table["delta_pulse_number"])
        except:
            self.toas.table["delta_pulse_number"] = np.zeros(len(self.toas.get_mjds()))
            delta_pulse_numbers = Phase(self.toas.table["delta_pulse_number"])

        # Track on pulse numbers, if requested
        if self.track_mode == "use_pulse_numbers":
            pulse_num = self.toas.get_pulse_numbers()
            if pulse_num is None:
                raise ValueError(
                    "Pulse numbers missing from TOAs but track_mode requires them"
                )
            # Compute model phase. For pulse numbers tracking
            # we need absolute phases, since TZRMJD serves as the pulse
            # number reference.
            modelphase = (
                self.model.phase(self.toas, abs_phase=True) + delta_pulse_numbers
            )
            # First assign each TOA to the correct relative pulse number, including
            # and delta_pulse_numbers (from PHASE lines or adding phase jumps in GUI)
            residualphase = modelphase - Phase(pulse_num, np.zeros_like(pulse_num))
            # This converts from a Phase object to a np.float128
            full = residualphase.int + residualphase.frac
        # If not tracking then do the usual nearest pulse number calculation
        else:
            # Compute model phase
            modelphase = self.model.phase(self.toas) + delta_pulse_numbers
            # Here it subtracts the first phase, so making the first TOA be the
            # reference. Not sure this is a good idea.
            if self.subtract_mean:
                modelphase -= Phase(modelphase.int[0], modelphase.frac[0])

            # Here we discard the integer portion of the residual and replace it with 0
            # This is effectively selecting the nearst pulse to compute the residual to.
            residualphase = Phase(np.zeros_like(modelphase.frac), modelphase.frac)
            # This converts from a Phase object to a np.float128
            full = residualphase.int + residualphase.frac
        # If we are using pulse numbers, do we really want to subtract any kind of mean?
        if not self.subtract_mean:
            return full
        if not self.use_weighted_mean:
            mean = full.mean()
        else:
            # Errs for weighted sum.  Units don't matter since they will
            # cancel out in the weighted sum.
            if np.any(self.toas.get_errors() == 0):
                raise ValueError(
                    "Some TOA errors are zero - cannot calculate residuals"
                )
            w = 1.0 / (self.toas.get_errors().value ** 2)
            mean, err = weighted_mean(full, w)
        return full - mean

    def calc_time_resids(self):
        """Return timing model residuals in time (seconds)."""
        if self.phase_resids is None:
            self.phase_resids = self.calc_phase_resids()
        return (self.phase_resids / self.get_PSR_freq()).to(u.s)

    def get_PSR_freq(self, modelF0=True):
        if modelF0:
            """Return pulsar rotational frequency in Hz. model.F0 must be defined."""
            if self.model.F0.units != "Hz":
                ValueError("F0 units must be Hz")
            # All residuals require the model pulsar frequency to be defined
            F0names = ["F0", "nu"]  # recognized parameter names, needs to be changed
            nF0 = 0
            for n in F0names:
                if n in self.model.params:
                    F0 = getattr(self.model, n).value
                    nF0 += 1
            if nF0 == 0:
                raise ValueError(
                    "no PSR frequency parameter found; "
                    + "valid names are %s" % F0names
                )
            if nF0 > 1:
                raise ValueError(
                    "more than one PSR frequency parameter found; "
                    + "should be only one from %s" % F0names
                )
            return F0 * u.Hz
        return self.model.d_phase_d_toa(self.toas)

    def calc_chi2(self, full_cov=False):
        """Return the weighted chi-squared for the model and toas.

        If the errors on the TOAs are independent this is a straightforward
        calculation, but if the noise model introduces correlated errors then
        obtaining a meaningful chi-squared value requires a Cholesky
        decomposition. This is carried out, here, by constructing a GLSFitter
        and asking it to do the chi-squared computation but not a fit.

        The return value here is available as self.chi2, which will not
        redo the computation unless necessary.

        The chi-squared value calculated here is suitable for use in downhill
        minimization algorithms and Bayesian approaches.

        Handling of problematic results - degenerate conditions explored by
        a minimizer for example - may need to be checked to confirm that they
        correctly return infinity.
        """
        if self.model.has_correlated_errors:
            # Use GLS but don't actually fit
            from pint.fitter import GLSFitter

            f = GLSFitter(self.toas, self.model, residuals=self)
            try:
                return f.fit_toas(maxiter=0, full_cov=full_cov)
            except LinAlgError as e:
                log.warning(
                    "Degenerate conditions encountered when "
                    "computing chi-squared: %s" % (e,)
                )
                return np.inf
        else:
            # Residual units are in seconds. Error units are in microseconds.
            if (self.toas.get_errors() == 0.0).any():
                return np.inf
            else:
                # The self.time_resids is in the unit of "s", the error "us".
                # This is more correct way, but it is the slowest.
                # return (((self.time_resids / self.toas.get_errors()).decompose()**2.0).sum()).value

                # This method is faster then the method above but not the most correct way
                # return ((self.time_resids.to(u.s) / self.toas.get_errors().to(u.s)).value**2.0).sum()

                # This the fastest way, but highly depend on the assumption of time_resids and
                # error units.
                # insure only a pure number returned
                try:
                    return (
                        ((self.time_resids / self.toas.get_errors().to(u.s)) ** 2.0)
                        .sum()
                        .value
                    )
                except:
                    return (
                        (self.time_resids / self.toas.get_errors().to(u.s)) ** 2.0
                    ).sum()

    def get_dof(self):
        """Return number of degrees of freedom for the model."""
        dof = self.toas.ntoas
        for p in self.model.params:
            dof -= bool(not getattr(self.model, p).frozen)
        # Now subtract 1 for the implicit global offset parameter
        # Note that we should do two things eventually
        # 1. Make the offset not be a hidden parameter
        # 2. Have a model object return the number of free parameters instead of having to count non-frozen parameters like above
        dof -= 1
        return dof

    def get_reduced_chi2(self):
        """Return the weighted reduced chi-squared for the model and toas."""
        return self.calc_chi2() / self.get_dof()

    def update(self):
        """Recalculate everything in residuals class after changing model or TOAs"""
        if self.toas is None or self.model is None:
            self.phase_resids = None
            self.time_resids = None
        if self.toas is None:
            raise ValueError("No TOAs provided for residuals update")
        if self.model is None:
            raise ValueError("No model provided for residuals update")

        self.phase_resids = self.calc_phase_resids()
        self.time_resids = self.calc_time_resids()
        self._chi2 = None  # trigger chi2 recalculation when needed
        self.dof = self.get_dof()

    def ecorr_average(self, use_noise_model=True):
        """
        Uses the ECORR noise model time-binning to compute "epoch-averaged"
        residuals.  Requires ECORR be used in the timing model.  If
        use_noise_model is true, the noise model terms (EFAC, EQUAD, ECORR) will
        be applied to the TOA uncertainties, otherwise only the raw
        uncertainties will be used.

        Returns a dictionary with the following entries:

          mjds           Average MJD for each segment

          freqs          Average topocentric frequency for each segment

          time_resids    Average residual for each segment, time units

          noise_resids   Dictionary of per-noise-component average residual

          errors         Uncertainty on averaged residuals

          indices        List of lists giving the indices of TOAs in the original
                         TOA table for each segment
        """

        # ECORR is required
        try:
            ecorr = self.model.get_components_by_category()["ecorr_noise"][0]
        except KeyError:
            raise ValueError("ECORR not present in noise model")

        # "U" matrix gives the TOA binning, "weight" is ECORR
        # uncertainty in seconds, squared.
        U, ecorr_err2 = ecorr.ecorr_basis_weight_pair(self.toas)
        ecorr_err2 *= u.s * u.s

        if use_noise_model:
            err = self.model.scaled_toa_uncertainty(self.toas)
        else:
            err = self.toas.get_errors()
            ecorr_err2 *= 0.0

        # Weight for sums, and normalization
        wt = 1.0 / (err * err)
        a_norm = np.dot(U.T, wt)

        def wtsum(x):
            return np.dot(U.T, wt * x) / a_norm

        # Weighted average of various quantities
        avg = {}
        avg["mjds"] = wtsum(self.toas.get_mjds())
        avg["freqs"] = wtsum(self.toas.get_freqs())
        avg["time_resids"] = wtsum(self.time_resids)
        avg["noise_resids"] = {}
        for k in self.noise_resids.keys():
            avg["noise_resids"][k] = wtsum(self.noise_resids[k])

        # Uncertainties
        # TODO could add an option to incorporate residual scatter
        avg["errors"] = np.sqrt(1.0 / a_norm + ecorr_err2)

        # Indices back into original TOA list
        avg["indices"] = [list(np.where(U[:, i])[0]) for i in range(U.shape[1])]

        return avg


class WidebandDMResiduals(Residuals):
    """ Residuals for independent DM measurement (i.e. Wideband TOAs).
    """

    def __init__(
        self,
        toas=None,
        model=None,
        residual_type="dm",
        unit=u.pc / u.cm ** 3,
        subtract_mean=True,
        use_weighted_mean=True,
        scaled_by_F0=False,
    ):

        self.toas = toas
        self.model = model
        self.residual_type = residual_type
        self.unit = unit
        self.subtract_mean = subtract_mean
        self.use_weighted_mean = use_weighted_mean
        self.base_unit = u.pc / u.cm ** 3
        self.get_model_value = self.model.dm_value
        self.dm_data, self.dm_error = self.get_dm_data()
        self.scaled_by_F0 = scaled_by_F0
        self._chi2 = None

    @property
    def resids(self):
        return self.calc_resids()

    @property
    def resids_value(self):
        """ Get pure value of the residuals use the given base unit.
        """
        return self.resids.to_value(self.unit)

    @property
    def data_error(self):
        return self.dm_error

    @property
    def chi2(self):
        """Compute chi-squared as needed and cache the result"""
        if self._chi2 is None:
            self._chi2 = self.calc_chi2()
        assert self._chi2 is not None
        return self._chi2

    def calc_resids(self):
        model_value = self.get_model_value(self.toas)
        resids = self.dm_data - model_value
        if self.subtract_mean:
            if not self.use_weighted_mean:
                resids -= resids.mean()
            else:
                # Errs for weighted sum.  Units don't matter since they will
                # cancel out in the weighted sum.
                if self.dm_error is None or np.any(self.dm_error == 0):
                    raise ValueError(
                        "Some DM errors are zero - cannot calculate the"
                        " weighted residuals."
                    )
                w = 1.0 / (self.dm_error ** 2)
                wm = (resids * w).sum() / w.sum()
                resids -= wm
        return resids

    def calc_chi2(self):
        if (self.data_error.value == 0.0).any():
            return np.inf
        else:
            try:
                return ((self.resids / self.data_error) ** 2.0).sum().decompose().value
            except:
                return ((self.resids / self.data_error) ** 2.0).sum().decompose()

    def rms_weighted(self):
        """Compute weighted RMS of the residals in time."""
        if np.any(self.data_error.value == 0):
            raise ValueError(
                "Some TOA errors are zero - cannot calculate weighted RMS of residuals"
            )
        w = 1.0 / (self.data_error ** 2)

        wmean, werr, wsdev = weighted_mean(self.resids, w, sdev=True)
        return wsdev

    def get_dm_data(self):
        """Get the independent measured DM data from TOA flags.

        Return
        ------
        valid_dm: `numpy.ndarray`
            Independent measured DM data from TOA line. It only returns the DM
            values that is present in the TOA flags.

        valid_error: `numpy.ndarray`
            The error associated with DM values in the TOAs.

        valide_index: list
            The TOA with DM data index.
        """
        dm_data, valid_data = self.toas.get_flag_value("pp_dm")
        dm_error, valid_error = self.toas.get_flag_value("pp_dme")
        if valid_data == []:
            raise ValueError("Input TOA object does not have wideband DM values")
        if valid_error == []:
            raise ValueError("Input TOA object does not have wideband DM errors")
        valid_dm = np.array(dm_data)[valid_data]
        valid_error = np.array(dm_error)[valid_error]
        # Check valid error, if an error is none, change it to zero
        if len(valid_dm) != len(valid_error):
            raise ValueError("Input TOA object' DM data and DM errors do not match.")
        return valid_dm * self.unit, valid_error * self.unit

    def update_model(self, new_model, **kwargs):
        """ Up date DM models from a new PINT timing model

        Parameters
        ----------
        new_model : `pint.timing_model.TimingModel`
        """

        self.model = new_model
        self.model_func = self.model.dm_value


residual_map = {"toa": Residuals, "dm": WidebandDMResiduals}


class CombinedResiduals(object):
    """ A class provides uniformed API that collects result from different type
    of residuals.

    Parameters
    ----------
    residuals: List of residual objects
        A list of different typs of residual objects

    Note
    ----
    Since different type of residuals has different of units. The overall
    residuals will have no units.
    """

    def __init__(self, residuals):
        self.residual_objs = residuals

    @property
    def resids(self):
        """ Residuals from all of the residual types.
        """
        all_resids = []
        for res in self.residual_objs:
            all_resids.append(res.resids_value)
        return np.hstack(all_resids)

    @property
    def unit(self):
        return [res.unit for res in self.residual_objs]

    @property
    def chi2(self):
        chi2 = 0
        for res in self.residual_objs:
            chi2 += res.chi2
        return chi2

    @property
    def data_error(self):
        # Since it is the combinde residual, the units are removed.
        dr = self.get_data_error()
        return np.hstack([rv.value for rv in dr.values()])

    def get_data_error(self):
        errors = []
        for rs in self.residual_objs:
            errors.append((rs.residual_type, rs.data_error))
        return collections.OrderedDict(errors)

    def rms_weighted(self):
        """Compute weighted RMS of the residals in time."""
        if np.any(self.data_error == 0):
            raise ValueError(
                "Some data errors are zero - cannot calculate weighted RMS of residuals"
            )
        w = 1.0 / (self.data_error ** 2)

        wmean, werr, wsdev = weighted_mean(self.resids, w, sdev=True)
        return wsdev
