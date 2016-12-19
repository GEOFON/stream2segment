#@PydevCodeAnalysisIgnore
'''
Created on Feb 23, 2016

@author: riccardo
'''

import mock, os, sys
import pytest
import re
import argparse
import numpy as np
import pandas as pd
from stream2segment.analysis import env, cumsum, fft
from scipy.signal import hilbert
from stream2segment.analysis.mseeds import remove_response #, loads as s2s_loads, dumps
from stream2segment.io.dataseries import loads, dumps
from stream2segment.analysis.mseeds import _IO_FORMAT_FFT, _IO_FORMAT_STREAM, _IO_FORMAT_TIME,\
    _IO_FORMAT_TRACE

from obspy.core.inventory import read_inventory
from obspy.core import read as obspy_read
from obspy.core import Trace, Stream
from StringIO import StringIO
from obspy.io.stationxml.core import _read_stationxml
from obspy.core.trace import Trace
from itertools import count
from stream2segment.analysis import fft as orig_fft, env



    
from scipy.signal import hilbert
@pytest.mark.parametrize('arr, expected_result, ',
                          [
                           ([-1, 1], [1, 1]),
                           ([-2, 3], [2, 3]),
                           ([-1, 10, -12, 3], [3.64005494, 11.41271221, 12.5, 6.26498204])
                           ],
                        )
@mock.patch('stream2segment.analysis.hilbert', side_effect=lambda *a,**k: hilbert(*a, **k))
@mock.patch('stream2segment.analysis.np')
def test_env(mock_np, mock_hilbert, arr, expected_result):
    mock_np.abs = mock.Mock(side_effect = lambda *a, **k: np.abs(*a, **k))
    r = env(arr)
    assert len(r) == len(arr)
    mock_hilbert.assert_called_once_with(arr)
    assert mock_np.abs.called
    g = 9    

@pytest.mark.parametrize('arr, normalize, expected_result, ',
                          [([-1, 1], True, [0.5, 1]),
                           ([-1, 1], False, [1, 2]),
                           ([-2, 3], True, [4.0/(4+9), (4+9.0)/(4+9)]),
                           ([-2, 3], False, [4, 4+9]),
                           ])
@mock.patch('stream2segment.analysis.np')
def test_cumsum(mock_np, arr, normalize, expected_result):
    mock_np.cumsum = mock.Mock(side_effect = lambda *a, **k: np.cumsum(*a, **k))
    mock_np.square =  mock.Mock(side_effect = lambda *a, **k: np.square(*a, **k))
    mock_np.max =  mock.Mock(side_effect = lambda *a, **k: np.max(*a, **k))
    mock_np.true_divide =  mock.Mock(side_effect = lambda *a, **k: np.true_divide(*a, **k))

    r = cumsum(arr, normalize=normalize)
    assert len(r) == len(arr)
    assert (r == np.array(expected_result)).all()
    assert mock_np.cumsum.called
    assert mock_np.square.called
    if normalize:
        assert mock_np.max.called
        assert mock_np.true_divide.called
    else:
        assert not mock_np.max.called
        assert not mock_np.true_divide.called