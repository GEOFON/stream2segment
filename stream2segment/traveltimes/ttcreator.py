'''
Module for creating a Travel times table object, a gird of points where
the minimum theoretical travel times can be later efficiently calculated by
using linear, cubic or nearest sample approximation

:date: Aug 23, 2017

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''
from __future__ import print_function, division

# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#explicit-imports):
from builtins import zip, next, range, object

import os
import sys
import math
from multiprocessing import Pool
import time
from datetime import timedelta
from itertools import count

import numpy as np
from obspy.geodetics.base import degrees2kilometers
from obspy.taup.utils import get_phase_names
from obspy.taup.helper_classes import SlownessModelError
from obspy.taup.tau_model import TauModel
from obspy.taup.taup_time import TauPTime

from stream2segment.utils import get_progressbar


# global vars
DEFAULT_SD_MAX = 700.0  # in km
DEFAULT_RD_MAX = 0.0  # in km
DEFAULT_DIST_MAX = 180.0  # in degrees
DEFAULT_PWAVEVELOCITY = 5  # in km/sec  PLEASE SPECIFY A FLOAT!!
DEFAULT_SWAVEVELOCITY = 3  # in km/sec  PLEASE SPECIFY A FLOAT!!
DEFAULT_DEG2KM = degrees2kilometers(1)


def timemaxdecimaldigits(time_err_tolerance):
    numdigits = 0
    _ = time_err_tolerance
    while int(_) != _:
        _ *= 10
        numdigits += 1
        if numdigits > 3:
            raise ValueError("MAX_TIME_ERR_TOL cannot be lower than 0.001 (one millisecond)")
    return numdigits


def min_traveltimes(modelname, source_depths, receiver_depths, distances, phases, callback=None):
    '''computes the minimum travel times using multiprocessing to speed up
    calculations
    min_traveltimes(modelname, ARRAY[N], ARRAY[N], SCALAR, ...) -> [N-length array]
    min_traveltimes(modelname, ARRAY[N], ARRAY[N], ARRAY[M]) -> [NxM] matrix
    min_traveltimes(modelname, SCALAR, SCALAR, ARRAY[M]) -> [M-length array]
    '''
    model = taumodel(modelname)
    source_depths, receiver_depths = np.broadcast_arrays(source_depths, receiver_depths)
    # assert we passed arrays, not matrices:
    if len(source_depths.shape) > 1 or len(distances.shape) > 1:
        raise ValueError("Need to have arrays, not matrices")
    norowdim = source_depths.ndim == 0
    if norowdim:
        source_depths = np.array([source_depths])
        receiver_depths = np.array([receiver_depths])
    nocoldim = distances.ndim == 0
    if nocoldim:
        distances = np.array([distances])
    ttimes = np.full(shape=(source_depths.shape[0], distances.shape[0]), fill_value=np.nan)

    def mp_callback(index, array, _callback=None):
        def _(result):
            array[index] = result
            if _callback is not None:
                _callback()
        return _

    pool = Pool()
    for idx, sdepth, rdepth in zip(count(), source_depths, receiver_depths):
        tmp_ttimes = ttimes[idx]
        for i, dist in enumerate(distances):
            pool.apply_async(min_traveltime, (model, sdepth, rdepth, dist, phases),
                             callback=mp_callback(i, tmp_ttimes, callback))
    pool.close()
    pool.join()

    if norowdim or nocoldim:
        ttimes = ttimes.flatten()

    return ttimes


def min_traveltime(model, source_depth_in_km, receiver_depth_in_km, distance_in_degree, phases):
    try:
        # little preamble for optimization:
        # given a model string, obspy allocates an `obspy.taup.tau.TauPyModel` object
        # the TauPyModel object has member `model` which is a `obspy.taup.tau_model.TauModel` object
        # the `TauModel` is used in `TauPyModel.get_travel_times` method, which is the method
        # we should call: instead, we write it here getting rid of the `TauPyModel` object
        # A further optimization is that our get_travel_times returns the minimum, thus
        # avoiding allocating an array of all arrival times, but returning the minimum
        # but this does not seem to dramatically increase performances, on the other hand is almost
        # inefficient. The only effective improvement is to pre-allocate
        # with  `get_taumodel(string)` and pass it here as model

        # allocate TauModel. We might pass a TauModel object or a string:
        tau_model = taumodel(model)

        tpt = TauPTime(tau_model, phases, source_depth_in_km,
                       distance_in_degree, receiver_depth_in_km)
        tpt.run()
        if not tpt.arrivals:
            return np.nan
        return tpt.arrivals[0].time

    except SlownessModelError:
        return np.nan


def taumodel(model):
    """Returns a TauModel from string, or the argument in case of TypeError, assuming the
    latter is already a TauModel
    """
    try:
        return TauModel.from_file(model)
        # NOTE from_file has an argument cache that we ignore because of a bug (reported)
        # in obspy 1.0.2 if cache is False!
    except TypeError:
        return model  # ok, we assume the argument is already a TaupModel then


def ttequal(traveltimes1, traveltimes2, maxerr):
    '''function defining if two traveltimes array are equal. Uses np.allclose'''
    return np.allclose(traveltimes1, traveltimes2, rtol=0, atol=maxerr, equal_nan=True)


def newarray(basearray):
    '''Returns a new array the same shape of `basearray` and filled with NaNs'''
    return np.full(basearray.shape, np.nan)


def _cmp_indices(distances):  # returns the indices used for comparison from the given array arg.
    return np.unique([0, 1, 5, 10, int(len(distances)/4), int(len(distances)/2),
                      int(3*len(distances)/4), len(distances)-1])


def linspace(endvalue, step):
    '''
    :return: a wrapper around linspace which accepts an end and a step. The latter
        is adjusted if endvalue/step is not integer. The returned array includes always
        `endvalue`
    '''
    # calculate the number of points:
    numpts = 1 + int(np.true_divide(endvalue, step))
    return np.linspace(0, endvalue, numpts, endpoint=True)


def getstep(tt_errtol, wavevelocity, deg2km=DEFAULT_DEG2KM, unit='deg'):
    # Calculate the distance step, in degrees, rounded to the third decimal place (min=0.001 deg)
    # this gives the maximum granularity of travel times every 110 meters (not below). Increase to 4
    # if you want to go down to 11 meters, and so on
    # space = (DIST_STEP * DEG2KM) / time = MAX_TIME_ERR_TOL = velocity = PWAVEVELOCITY
    dist_step = np.around(float(wavevelocity * tt_errtol), 3)
    if unit == 'deg':  # convert to degree (approx)
        dist_step = np.around(np.true_divide(dist_step, deg2km), 3)
    return dist_step


class StepIterator(object):
    '''An iterator which can move back and decrease the iteration step until a maximum
    depth is reached'''
    def __init__(self, start, end, step):
        self._start = start
        self._end = end
        # re-adjust the step if greater than end-start, so that we loop thorugh
        # the bounds with some granularity. Without this, we would just return [start]
        # (if start == end) or [start, end]. This iterator is assumed to scan the bounds
        # more than that. This has to be done if end > start. If we set start = end = 0,
        # we do not bother about the step as we will yield only start
        if step > end - start and end > start:
            step = end - start
        if step == end - start:
            step /= 10
        self._step = step
        _ = int(math.log10(step))
        self._iterindices = [_, _-1, _-2]
        self._iterindex = 0
        self._itr = None
        self._val = start
        self._raisestop = False
        self._refreshitr()

    def _refreshitr(self):
        self._itr = count(self._val, self.currentstep)

    @property
    def currentstep(self):
        return 10 ** self._iterindices[self._iterindex]

    def moveback(self):
        '''Moves the iterator back of one value and
        decreases the step'''
        # outofbounds:
        if self._val - self.currentstep < self._start or self._raisestop:
            return False  # we cannot move back

        if self._iterindex == len(self._iterindices) - 1:  # no more depth available
            self._iterindex = 0
            self._refreshitr()
            next(self._itr)  # hack: move forward as first _itr item has already been yielded
            return False  # we cannot move back

        # move to next depth level
        self._val -= self.currentstep
        self._iterindex += 1
        self._refreshitr()  # hack: move forward as first _itr item has already been yielded
        next(self._itr)
        return True

    def __iter__(self):
        return self

    def __next__(self):
        if self._raisestop:
            raise StopIteration
        self._val = next(self._itr)
        if self._val >= self._end:
            self._val = self._end
            self._raisestop = True
        return self._val

    # next = __next__  # Not needed (see from builtins import object)


def itercreator(model, tt_errtol, phases, distances, depthstep_km, maxsourcedepth=DEFAULT_SD_MAX,
                maxreceiverdepth=DEFAULT_RD_MAX):
    # the indices used for comparison (calculate only a portion of distances for speed reasons)
    # Be more granular for small distances (FIXME: why?)
    cmp_indices = _cmp_indices(distances)
    # and relative distances to be used for calculation:
    cmp_distances_in_degree = distances[cmp_indices]

    # as numpy allclose returns true if the error is <= max_abs_err_tol_in_sec, we want a strict
    # inequality, thus:
    max_err = abs(tt_errtol)

    model = taumodel(model)
    last_saved_traveltimes = []

    sditer = StepIterator(0, maxsourcedepth, depthstep_km)
    for sdepth in sditer:
        # calculate the travel times anyway
        ttimes1 = min_traveltimes(model, sdepth, 0, cmp_distances_in_degree, phases)
        ttimes2 = ttimes1 if maxreceiverdepth == 0 else \
            min_traveltimes(model, sdepth, maxreceiverdepth, cmp_distances_in_degree, phases)

        if sdepth not in (0, maxsourcedepth):
            # first thing to do: error exceeds with previous travel times, go on if we can
            if not ttequal(last_saved_traveltimes[0], ttimes1, max_err):
                if sditer.moveback():
                    continue
            elif ttequal(last_saved_traveltimes[1], ttimes2, max_err) and \
                    ttequal(ttimes1, ttimes2, max_err):
                continue

        if maxreceiverdepth == 0:
            yield sdepth, 0, ttimes1, None if not last_saved_traveltimes else \
                last_saved_traveltimes[0]
            last_saved_traveltimes = [ttimes1, ttimes2]
            continue

        # ok, now we have to store ttimes1 and ttimes2. We iterate over receiver depths to check
        # when we need to store points
        rditer = StepIterator(0, maxreceiverdepth, depthstep_km)
        _markttimes = None if not last_saved_traveltimes else last_saved_traveltimes[0]
        for rdepth in rditer:
            if rdepth in (0, maxreceiverdepth):
                _markttimes = ttimes1 if rdepth == 0 else ttimes2
                yield sdepth, rdepth, _markttimes, \
                    None if not last_saved_traveltimes else last_saved_traveltimes[0]
                continue

            ttimes3 = min_traveltimes(model, sdepth, rdepth, cmp_distances_in_degree, phases)

            # first thing to do: error exceeds with previous travel times, go on if we can
            if not ttequal(ttimes3, _markttimes, max_err):
                if rditer.moveback():
                    continue
                else:
                    yield sdepth, rdepth, ttimes3, _markttimes
                    _markttimes = ttimes3

        last_saved_traveltimes = [ttimes1, ttimes2]


def minwavevelocity(phases, pwavevelocity=DEFAULT_PWAVEVELOCITY,
                    swavevelocity=DEFAULT_SWAVEVELOCITY):
    ttp_plus = set(get_phase_names("ttp+"))
    for phase_name in phases:
        for phname in get_phase_names(phase_name):
            if phname not in ttp_plus:
                return swavevelocity
    # s-waves are slower, so if present return them to create delta steps
    # including also P-waves, if any.
    return pwavevelocity


def get_sdrd_steps(model, tt_errtol, phases, maxsourcedepth=DEFAULT_SD_MAX,
                   maxreceiverdepth=DEFAULT_RD_MAX, maxdistance=DEFAULT_DIST_MAX,
                   pwavevelocity=DEFAULT_PWAVEVELOCITY, swavevelocity=DEFAULT_SWAVEVELOCITY,
                   deg2km=DEFAULT_DEG2KM, isterminal=False):
    start = time.time()
    data = []
    sds, rds = [], []
    wavevelocity = minwavevelocity(phases, pwavevelocity, swavevelocity)
    # calculate distances array:
    distances = linspace(maxdistance, getstep(tt_errtol, wavevelocity, deg2km, unit='deg'))

    # get the indices used for comparison in our algorithm
    cmp_indices = _cmp_indices(distances)
    if isterminal:
        print("Calculating source and receiver depth steps")
        print("- The algorithm basically iterates over each source depth and receiver depths pair")
        print("  and saves the associated travel times array (t)")
        print("  when it exceeds %f seconds with the previous computed t" % tt_errtol)
        print("- tc below refers to a chunk of each saved t showing the values at distances (deg):")
        print("  %s" % (np.around(distances[cmp_indices], decimals=3).tolist()))

        # typical header should be (roughly):
        # src_depth  rec_depth    travel times (max err with previous)   done              eta
        #        0.0        0.0  [0.0, 21.088, 36.402, 50.154] (0.000)  0.02%  19:15:33.394541
        # ...

        frmt = "%4s %9s %9s %60s %5s %16s"
        header = frmt % ('#', 'src_depth', 'rec_depth', 'tc (max err with previous tc)', '%done',
                         'eta')
        print("-"*len(header))
        print(header)
        print("-"*len(header))

    # pool = Pool()
    # already_calculated_tt_indices = set(_CMP_DIST_INDICES)
    idx = 1
    total = maxsourcedepth
    depthstep_km = getstep(tt_errtol, wavevelocity, deg2km, unit='km')
    for sdepth, rdepth, tts, lasttts in itercreator(model, tt_errtol, phases,
                                                    distances, depthstep_km,
                                                    maxsourcedepth, maxreceiverdepth):
        if isterminal:
            count_ = sdepth / total
            percentdone = int(0.5 + (100.0 * count_))
            eta = None if count_ == 0 else \
                int(0.5 + (total-sdepth) * ((time.time() - start) / sdepth))
            maxerr = np.nan if lasttts is None else np.nanmax(abs(tts-lasttts))
            # round to 0.1 sec:
            tt_list = np.around(tts, decimals=timemaxdecimaldigits(tt_errtol)+1).tolist()
            print(frmt % (idx, np.around(sdepth, 3),
                          np.around(rdepth, 3), '%s (%8.3f)' % (tt_list, maxerr), percentdone,
                          "n/a" if eta is None else str(timedelta(seconds=eta))))
            idx += 1

        sds.append(sdepth)
        rds.append(rdepth)

        # NOW compute all remaining travel times. Use apply async so that we can go on inspecting
        # new source, receiver pairs
        complete_tts = newarray(distances)
        data.append(complete_tts)
        # add already calculated:
        complete_tts[cmp_indices] = tts

    return np.array(sds), np.array(rds), distances, \
        np.reshape(data, newshape=(len(data), len(data[0])))


def computetts(model, sourcedepths, receiverdepths, distances, tts_matrix, phases,
               isterminal=False):
    model = taumodel(model)
    numtts = tts_matrix.shape[0]
    _mask = np.isnan(tts_matrix[0])
    pts2computepercol = _mask.sum()
    pts2compute = pts2computepercol * numtts
    print("Calculating remaining travel times points:")
    print("(the algorithm re-computes the travel time for points that are nan)")
    print("%d traveltimes arrays found" % numtts)
    print("%d points to compute for each array" % pts2computepercol)
    print("%d total points to compute" % pts2compute)

    pool = Pool()
    with get_progressbar(isterminal, length=pts2compute) as pbar:
        _tts_matrix = min_traveltimes(model, sourcedepths, receiverdepths, distances[_mask],
                                      phases, callback=lambda: pbar.update(1))
        pool.close()
        pool.join()

    for i in range(numtts):
        tts_matrix[i, _mask] = _tts_matrix[i, :]


def computeall(fileout, model, tt_errtol, phases, maxsourcedepth=DEFAULT_SD_MAX,
               maxreceiverdepth=DEFAULT_RD_MAX, maxdistance=DEFAULT_DIST_MAX,
               pwavevelocity=DEFAULT_PWAVEVELOCITY, swavevelocity=DEFAULT_SWAVEVELOCITY,
               deg2km=DEFAULT_DEG2KM, isterminal=True):
    if not os.path.isdir(os.path.dirname(fileout)):
        raise OSError("File directory does not exist: '%s'" % str(os.path.dirname(fileout)))

    modelname = model
    if isterminal:
        print("Computing and saving travel times table to '%s'" % str(fileout))
        print("  model:  '%s'" % modelname)
        print("  phases: '%s'" % str(phases))
    model = taumodel(modelname)
    sdepths, rdepths, dists, tt_matrix = get_sdrd_steps(model, tt_errtol, phases, maxsourcedepth,
                                                        maxreceiverdepth, maxdistance,
                                                        pwavevelocity, swavevelocity, deg2km,
                                                        isterminal)
    tt_matrix = tt_matrix.astype(np.float32)
    if isterminal:
        print("")
    kwargs = dict(file=fileout, modelname=modelname, sourcedepth_bounds_km=[0, maxsourcedepth],
                  receiverdepth_bounds_km=[0, maxreceiverdepth],
                  distances_bounds_deg=[dists[0], dists[-1]],
                  distances_step_deg=dists[1]-dists[0],
                  tt_errtol=tt_errtol, distances=dists,
                  pwave_velocity=pwavevelocity, swave_velocity=swavevelocity, deg2km=deg2km,
                  sourcedepths=sdepths, receiverdepths=rdepths, traveltimes=tt_matrix,
                  phases=phases)
    # save now so in case we can interrupt somehow the computation (?)
    np.savez_compressed(**kwargs)
    computetts(model, sdepths, rdepths, dists, tt_matrix, phases, isterminal=True)
    # save:
    np.savez_compressed(**kwargs)
    if isterminal:
        print("Computed %d travel times arrays associated to "
              "%d (source_depth, receiver_depth) pairs" % (tt_matrix.shape[0], tt_matrix.shape[0]))
        print("Each travel times array has %d points" % tt_matrix.shape[1])
        print("All travel times arrays compose a %dx%d matrix where:" %
              (tt_matrix.shape[0], tt_matrix.shape[1]))
        print("  a row represents a given (source_depth, receiver_depth) pair")
        print("  a column represents a given distance (in degree)")
        herr = np.abs(np.diff(tt_matrix, axis=1))
        verr = np.abs(np.diff(tt_matrix, axis=0))
        print("Time errors:")
        print("           %8s %8s %8s %8s" % ("min", "median", "mean", "max"))
        print("----------------------------------------------")
        print("horizontal %8.3f %8.3f %8.3f %8.3f (all in sec)" %
              (np.min(herr), np.median(herr), np.mean(herr), np.max(herr)))
        print("vertical   %8.3f %8.3f %8.3f %8.3f (all in sec)" %
              (np.min(verr), np.median(verr), np.mean(verr), np.max(verr)))
        print("(horizontal: time difference between two adjacent points on same row)")
        print("(vertical: time difference between two adjacent points on same column)")
        print("Bounds:")
        print("Source depths [min, max]: [%.3f, %.3f] km" % (0, maxsourcedepth))
        print("Receiver depths [min, max]: [%.3f, %.3f] km" % (0, maxreceiverdepth))
        print("Distances [min : step: max]: [%.3f: %.3f: %.3f] deg" %
              (0, dists[1]-dists[0], dists[-1]))
        print("Travel times table written to '%s'" % fileout)
        print("")


def _filepath(fileout, model, phases):
    if os.path.isdir(fileout):
        fileout = os.path.join(fileout, model + "_" + "_".join(phases))
    return fileout
