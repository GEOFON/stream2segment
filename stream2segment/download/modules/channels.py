'''
Download module for stations (level=channel) download

:date: Dec 3, 2017

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''
# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#explicit-imports):
from builtins import map, next, zip, range, object

import logging
from itertools import cycle

import numpy as np
import pandas as pd
from sqlalchemy import or_, and_

from stream2segment.io.db.models import DataCenter, Station, Channel
from stream2segment.download.utils import read_async, response2normalizeddf, empty, QuitDownload,\
    handledbexc, dbsyncdf
from stream2segment.utils.msgs import MSG
from stream2segment.utils import get_progressbar, strconvert
from stream2segment.io.db.pdsql import dbquery2df, shared_colnames, mergeupdate

# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#aliased-imports):
from future import standard_library
standard_library.install_aliases()
from urllib.parse import urlparse  # @IgnorePep8
from urllib.request import Request  # @IgnorePep8


# logger: do not use logging.getLogger(__name__) but point to stream2segment.download.logger:
# this way we preserve the logging namespace hierarchy
# (https://docs.python.org/2/howto/logging.html#advanced-logging-tutorial) when calling logging
# functions of stream2segment.download.utils:
from stream2segment.download import logger  # @IgnorePep8


def get_channels_df(session, datacenters_df, eidavalidator,  # <- can be none
                    channels, starttime, endtime,
                    min_sample_rate, update,
                    max_thread_workers, timeout, blocksize, db_bufsize,
                    show_progress=False):
    """Returns a dataframe representing a query to the eida services (or the internal db
    if `post_data` is None) with the given argument.  The
    dataframe will have as columns the `key` attribute of any of the following db columns:
    ```
    [Channel.id, Station.latitude, Station.longitude, Station.datacenter_id]
    ```
    :param datacenters_df: the first item resulting from `get_datacenters_df` (pandas DataFrame)
    :param post_data: the second item resulting from `get_datacenters_df` (string)
    :param channels: a list of string denoting the channels, or None for no filtering
    (all channels). Each string follows FDSN specifications (e.g. 'BHZ', 'H??'). This argument
    is not used if `post_data` is given (not None)
    :param min_sample_rate: minimum sampling rate, set to negative value for no-filtering
    (all channels)
    """
    postdata = "* * * %s %s %s" % (",".join(channels) if channels else "*",
                                   "*" if not starttime else starttime.isoformat(),
                                   "*" if not endtime else endtime.isoformat())
    ret = []
    url_failed_dc_ids = []
    iterable = ((id_, Request(url,
                              data=('format=text\nlevel=channel\n'+post_data_str).encode('utf8')))
                for url, id_, post_data_str in zip(datacenters_df[DataCenter.station_url.key],
                                                   datacenters_df[DataCenter.id.key],
                                                   cycle([postdata])))

    with get_progressbar(show_progress, length=len(datacenters_df)) as bar:
        for obj, result, exc, url in read_async(iterable, urlkey=lambda obj: obj[-1],
                                                blocksize=blocksize,
                                                max_workers=max_thread_workers,
                                                decode='utf8', timeout=timeout):
            bar.update(1)
            dcen_id = obj[0]
            if exc:
                url_failed_dc_ids.append(dcen_id)
                logger.warning(MSG("Unable to fetch stations", exc, url))
            else:
                try:
                    df = response2normalizeddf(url, result[0], "channel")
                except ValueError as exc:
                    logger.warning(MSG("Discarding response data", exc, url))
                    df = empty()
                if not empty(df):
                    df[Station.datacenter_id.key] = dcen_id
                    ret.append(df)

    db_cha_df = pd.DataFrame()
    if url_failed_dc_ids:  # if some datacenter does not return station, warn with INFO
        dc_df_fromdb = datacenters_df.loc[datacenters_df[DataCenter.id.key].isin(url_failed_dc_ids)]
        logger.info(MSG("Fetching stations from database for %d (of %d) data-center(s)",
                    "download errors occurred") %
                    (len(dc_df_fromdb), len(datacenters_df)) + ":")
        logger.info(dc_df_fromdb[DataCenter.dataselect_url.key].to_string(index=False))
        db_cha_df = get_channels_df_from_db(session, dc_df_fromdb, channels, starttime, endtime,
                                            min_sample_rate, db_bufsize)

    # build two dataframes which we will concatenate afterwards
    web_cha_df = pd.DataFrame()
    if ret:  # pd.concat complains for empty list
        web_cha_df = pd.concat(ret, axis=0, ignore_index=True, copy=False)
        # remove unmatching sample rates:
        if min_sample_rate > 0:
            srate_col = Channel.sample_rate.key
            oldlen, web_cha_df = len(web_cha_df), \
                web_cha_df[web_cha_df[srate_col] >= min_sample_rate]
            discarded_sr = oldlen - len(web_cha_df)
            if discarded_sr:
                logger.warning(MSG("%d channel(s) discarded",
                                   "sample rate < %s Hz" % str(min_sample_rate)),
                               discarded_sr)
            if web_cha_df.empty and db_cha_df.empty:
                raise QuitDownload("No channel found with sample rate >= %f" % min_sample_rate)

        try:
            # this raises QuitDownload if we cannot save any element:
            web_cha_df = save_stations_and_channels(session, web_cha_df, eidavalidator, update,
                                                    db_bufsize)
        except QuitDownload as qexc:
            if db_cha_df.empty:
                raise
            else:
                logger.warning(qexc)

    if web_cha_df.empty and db_cha_df.empty:
        # ok, now let's see if we have remaining datacenters to be fetched from the db
        raise QuitDownload(Exception(MSG("No station found",
                                     ("Unable to fetch stations from all data-centers, "
                                      "no data to fetch from the database. "
                                      "Check config and log for details"))))

    # the columns for the channels dataframe that will be returned
    colnames = [c.key for c in [Channel.id, Channel.station_id, Station.latitude,
                                Station.longitude, Station.datacenter_id, Station.start_time,
                                Station.end_time, Station.network, Station.station,
                                Channel.location, Channel.channel]]
    if db_cha_df.empty:
        return web_cha_df[colnames]
    elif web_cha_df.empty:
        return db_cha_df[colnames]
    else:
        return pd.concat((web_cha_df, db_cha_df), axis=0, ignore_index=True)[colnames].copy()


def get_channels_df_from_db(session, datacenters_df, channels, starttime, endtime, min_sample_rate,
                            db_bufsize):
    # _be means "binary expression" (sql alchemy object reflecting a sql clause)
    cha_be = or_(*[Channel.channel.like(strconvert.wild2sql(cha)) for cha in channels]) \
        if channels else True
    srate_be = Channel.sample_rate >= min_sample_rate if min_sample_rate > 0 else True
    # select only relevant datacenters. Convert tolist() cause python3 complains of numpy ints
    # (python2 doesn't but tolist() is safe for both):
    dc_be = Station.datacenter_id.in_(datacenters_df[DataCenter.id.key].tolist())
    # Starttime and endtime below: it must NOT hold:
    # station.endtime <= starttime OR station.starttime >= endtime
    # i.e. it MUST hold the negation:
    # station.endtime > starttime AND station.starttime< endtime
    stime_be = ((Station.end_time == None) | (Station.end_time > starttime)) if starttime else True  # @IgnorePep8
    # endtime: Limit to metadata epochs ending on or before the specified end time.
    # Note that station's ent_time can be None
    etime_be = (Station.start_time < endtime) if endtime else True  # @IgnorePep8
    sa_cols = [Channel.id, Channel.station_id, Station.latitude, Station.longitude,
               Station.start_time, Station.end_time, Station.datacenter_id, Station.network,
               Station.station, Channel.location, Channel.channel]
    # note below: binary expressions (all variables ending with "_be") might be the boolean True.
    # SqlAlchemy seems to understand them as long as they are preceded by a "normal" binary
    # expression. Thus q.filter(binary_expr & True) works and it's equal to q.filter(binary_expr),
    # BUT .filter(True & True) is not working as a no-op filter, it simply does not work
    qry = session.query(*sa_cols).join(Channel.station).filter(and_(dc_be, srate_be, cha_be,
                                                                    stime_be, etime_be))
    return dbquery2df(qry)


def save_stations_and_channels(session, channels_df, eidavalidator, update, db_bufsize):
    """
        Saves to db channels (and their stations) and returns a dataframe with only channels saved
        The returned data frame will have the column 'id' (`Station.id`) renamed to
        'station_id' (`Channel.station_id`) and a new 'id' column referring to the Channel id
        (`Channel.id`)
        :param channels_df: pandas DataFrame resulting from `get_channels_df`
    """
    # define columns (sql-alchemy model attrs) and their string names (pandas col names) once:
    STA_NET = Station.network.key
    STA_STA = Station.station.key
    STA_STIME = Station.start_time.key
    STA_DCID = Station.datacenter_id.key
    STA_ID = Station.id.key
    CHA_STAID = Channel.station_id.key
    CHA_LOC = Channel.location.key
    CHA_CHA = Channel.channel.key
    # set columns to show in the log on error (no row written):
    STA_ERRCOLS = [STA_NET, STA_STA, STA_STIME, STA_DCID]
    CHA_ERRCOLS = [STA_NET, STA_STA, CHA_LOC, CHA_CHA, STA_STIME, STA_DCID]
    # define a pre-formatteed string to log.info to in case od duplicates:
    infomsg = "Found {:d} {} to be discarded (checked against %s)" % \
        ("already saved stations: eida routing service n/a" if eidavalidator is None else
         "eida routing service response")
    # first drop channels of same station:
    sta_df = channels_df.drop_duplicates(subset=[STA_NET, STA_STA, STA_STIME, STA_DCID]).copy()
    # then check dupes. Same network, station, starttime but different datacenter:
    duplicated = sta_df.duplicated(subset=[STA_NET, STA_STA, STA_STIME], keep=False)
    if duplicated.any():
        sta_df_dupes = sta_df[duplicated]
        if eidavalidator is not None:
            keep_indices = []
            for _, group_df in sta_df_dupes.groupby(by=[STA_NET, STA_STA, STA_STIME],
                                                    sort=False):
                gdf = group_df.sort_values([STA_DCID])  # so we take first dc returning True
                for i, d, n, s, l, c in zip(gdf.index, gdf[STA_DCID], gdf[STA_NET], gdf[STA_STA],
                                            gdf[CHA_LOC], gdf[CHA_CHA]):
                    if eidavalidator.isin(d, n, s, l, c):
                        keep_indices.append(i)
                        break
            sta_df_dupes = sta_df_dupes.loc[~sta_df_dupes.index.isin(keep_indices)]
        else:
            sta_df_dupes.is_copy = False
            sta_df_dupes[STA_DCID + "_tmp"] = sta_df_dupes[STA_DCID].copy()
            sta_df_dupes[STA_DCID] = np.nan
            sta_db = dbquery2df(session.query(Station.network, Station.station, Station.start_time,
                                              Station.datacenter_id))
            mergeupdate(sta_df_dupes, sta_db, [STA_NET, STA_STA, STA_STIME], [STA_DCID])
            sta_df_dupes = sta_df_dupes[sta_df_dupes[STA_DCID] != sta_df_dupes[STA_DCID + "_tmp"]]

        if not sta_df_dupes.empty:
            exc_msg = "duplicated station(s)"
            logger.info(infomsg.format(len(sta_df_dupes), exc_msg))
            # print the removed dataframe to log.warning (showing STA_ERRCOLS only):
            handledbexc(STA_ERRCOLS)(sta_df_dupes.sort_values(by=[STA_NET, STA_STA, STA_STIME]),
                                     Exception(exc_msg))
            # https://stackoverflow.com/questions/28901683/pandas-get-rows-which-are-not-in-other-dataframe:
            sta_df = sta_df.loc[~sta_df.index.isin(sta_df_dupes.index)]

    # remember: dbsyncdf raises a QuitDownload, so no need to check for empty(dataframe)
    # also, if update is True, for stations only it must NOT update inventories HERE (handled later)
    _update_stations = update
    if _update_stations:
        _update_stations = [_ for _ in shared_colnames(Station, sta_df, pkey=False)
                            if _ != Station.inventory_xml.key]
    sta_df = dbsyncdf(sta_df, session, [Station.network, Station.station, Station.start_time],
                      Station.id, _update_stations, buf_size=db_bufsize, drop_duplicates=False,
                      cols_to_print_on_err=STA_ERRCOLS)
    # sta_df will have the STA_ID columns, channels_df not: set it from the former to the latter:
    channels_df = mergeupdate(channels_df, sta_df, [STA_NET, STA_STA, STA_STIME, STA_DCID],
                              [STA_ID])
    # rename now 'id' to 'station_id' before writing the channels to db:
    channels_df.rename(columns={STA_ID: CHA_STAID}, inplace=True)
    # check dupes and warn:
    channels_df_dupes = channels_df[channels_df[CHA_STAID].isnull()]
    if not channels_df_dupes.empty:
        exc_msg = "duplicated channel(s)"
        logger.info(infomsg.format(len(channels_df_dupes), exc_msg))
        # do not print the removed dataframe to log.warning (showing CHA_ERRCOLS only)
        # the info is redundant given the already removed stations. Left commented in any case:
        # handledbexc(CHA_ERRCOLS)(channels_df_dupes, Exception(exc_msg))
        channels_df.dropna(axis=0, subset=[CHA_STAID], inplace=True)
    # add channels to db:
    channels_df = dbsyncdf(channels_df, session,
                           [Channel.station_id, Channel.location, Channel.channel],
                           Channel.id, update, buf_size=db_bufsize, drop_duplicates=False,
                           cols_to_print_on_err=CHA_ERRCOLS)
    return channels_df