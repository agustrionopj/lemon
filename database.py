#! /usr/bin/env python

# Copyright (c) 2012 Victor Terron. All rights reserved.
# Institute of Astrophysics of Andalusia, IAA-CSIC
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division

"""
This module implements LEMONdB, the interface to the databases to which
photometric information (photometry.py), light curves (diffphot.py) and star
periods (periods.py) are saved. These databases contain all the information
relative to the campaign that may be needed for the data analysis process.

"""

import collections
import copy
import math
import numpy
import operator
import os
import random
import string
import sqlite3
import tempfile

# LEMON modules
import methods
import passband
import xmlparse

class DBStar(object):
    """ Encapsulates the instrumental photometric information for a star.

    This class is used as a container for the instrumental photometry of a
    star, observed in a specific photometric filter. It implements both
    high-level and low-level routines, the latter of which are fundamental for
    a scalable implementation of the differential photometry algorithms.

    """

    def __init__(self, id_, pfilter, phot_info, times_indexes,
                 dtype = numpy.longdouble):
        """ Instantiation method for the DBStar class.

        This is an abrupt descent in the abstraction ladder, but needed in
        order to compute the light curves fast and minimize the memory usage.

        Arguments:
        id_ - the ID of the star in the LEMONdB.
        pfilter - the photometric filter of the information being stored.
        phot_info - a two-dimensional NumPy array with the photometric
                    information. It *must* have three rows (the first for the
                    time, the second for the magnitude and the last for the
                    SNR) and as many columns as records for which there is
                    photometric information. For example, in order to get the
                    magnitude of the third image, we would do phot_info[1][2]
        times_indexes - a dictionary mapping each Unix time for which the star
                        was observed to its index in phot_info; this gives us
                        O(1) lookups when 'trimming' an instance. See the
                        BDStar.issubset and complete_for for further
                        information. Note that the values in this dictionary
                        are trusted blindly, so they better have the correct
                        values for phot_info!

        """

        self.id = id_
        self.pfilter = pfilter
        if phot_info.shape[0] != 3: # number of rows
            raise ValueError("'phot_info' must have exactly three rows")
        self._phot_info = phot_info
        self._time_indexes = times_indexes
        self.dtype = dtype

    def __str__(self):
        """ The 'informal' string representation """
        return "%s(ID = %d, filter = %s, %d records)" % \
               (self.__class__.__name__, self.id, self.pfilter, len(self))

    def __len__(self):
        """ Return the number of records for the star """
        return self._phot_info.shape[1] # number of columns

    def time(self, index):
        """ Return the Unix time of the index-th record """
        return self._phot_info[0][index]

    def _time_index(self, unix_time):
        """ Return the index of the Unix time in '_phot_info' """
        return self._time_indexes[unix_time]

    def mag(self, index):
        """ Return the magnitude of the index-th record """
        return self._phot_info[1][index]

    def snr(self, index):
        """ Return the SNR of the index-th record """
        return self._phot_info[2][index]

    @property
    def _unix_times(self):
        """ Return the Unix times at which the star was observed """
        return self._phot_info[0]

    def issubset(self, other):
        """ Return True if for each Unix time at which 'self' was observed,
        there is also an observation for 'other'; False otherwise """

        for unix_time in self._unix_times:
            if unix_time not in other._time_indexes:
                return False
        return True

    def _trim_to(self, other):
        """ Return a new DBStar which contains the records of 'self' that were
        observed at the Unix times that can be found in 'other'. KeyError will
        be raised if self if not a subset of other -- so you should check for
        that before trimming anything"""

        phot_info = numpy.empty((3, len(other)), dtype = self.dtype)
        for oindex, unix_time in enumerate(other._unix_times):
            sindex = self._time_index(unix_time)
            phot_info[0][oindex] = self.time(sindex)
            phot_info[1][oindex] = self.mag(sindex)
            phot_info[2][oindex] = self.snr(sindex)
        return DBStar(self.id, self.pfilter, phot_info,
                      other._time_indexes, dtype = self.dtype)

    def complete_for(self, iterable):
        """ Iterate over the supplied DBStars and trim them.

        The method returns a list with the 'trimmed' version of those DBStars
        which are different than 'self' (i.e., a star instance will not be
        considered to be a subset of itself) and of which it it is a subset.

        """

        complete_stars = []
        for star in iterable:
            if self is not star and self.issubset(star):
                complete_stars.append(star._trim_to(self))
        return complete_stars

    @staticmethod
    def make_star(id_, pfilter, rows, dtype = numpy.longdouble):
        """ Construct a DBstar instance for some photometric data.

        Feeding the class constructor with NumPy arrays and dictionaries is not
        particularly practical, so most of the time you may want to use instead
        this convenience function. It also receives the star ID and the filter
        of the star, but the photometric records are given as a sequence of
        three-element tuples (Unix time, magnitude and SNR).

        """

        # NumPy arrays are stored in contiguous blocks of memory, so adding
        # rows or columns to an existing one would require to copy the entire
        # array to a new block of memory. It is much better to first create an
        # array as big as will be needed -- numpy.empty(), unlike zeros, does
        # not initializes its entries and may therefore be marginally faster

        phot_info = numpy.empty((3, len(rows)), dtype = dtype)

        # A cache, mapping each Unix time to its index in phot_info; passed
        # to the constructor of DBStar for O(1) lookups of Unix times
        times_indexes = {}

        for index, row in enumerate(rows):
            unix_time, magnitude, snr = row
            phot_info[0][index] = unix_time
            phot_info[1][index] = magnitude
            phot_info[2][index] = snr
            times_indexes[unix_time] = index
        return DBStar(id_, pfilter, phot_info, times_indexes, dtype = dtype)


# The parameters used for aperture photometry
typename = 'PhotometricParameters'
field_names = "aperture, annulus, dannulus"
PhotometricParameters = collections.namedtuple(typename, field_names)

class ReferenceImage(object):
    """ Encapculates the image used for the offsets calculation """
    def __init__(self, path, pfilter, unix_time, object_, airmass, gain):
        self.path = path
        self.pfilter  = pfilter
        self.unix_time = unix_time
        self.object = object_
        self.airmass = airmass
        self.gain = gain


# A FITS image
typename = 'Image'
field_names = "path pfilter unix_time object airmass gain ra dec"
Image = collections.namedtuple(typename, field_names)

class LightCurve(object):
    """ The data points of a graph of light intensity of a celestial object.

    Encapsulates a series of Unix times linked to a differential magnitude with
    a signal-to-noise ratio. Internally stored as a list of three-element
    tuples, but we are implementing the add method so that we can interact with
    it as if it were a set, moving us up one level in the abstraction ladder.

    """

    def __init__(self, pfilter, cstars, cweights, dtype = numpy.longdouble):
        """ 'cstars' is a sequence or iterable of the IDs in the LEMONdB of the
        stars that were used as comparison stars when the light curve was
        computed, while 'cweights' is another sequence or iterable with the
        corresponding weights. The i-th comparison star (cstars) is assigned
        the i-th weight (cweights). The sum of all weights should equal one.

        """

        if len(cstars) != len(cweights):
            msg = "number of weights must equal that of comparison stars"
            raise ValueError(msg)
        if not cstars:
            msg = "at least one comparison star is needed"
            raise ValueError(msg)

        self._data = []
        self.pfilter = pfilter
        self.cstars = cstars
        self.cweights = cweights
        self.dtype = dtype

    def add(self, unix_time, magnitude, snr):
        """ Add a data point to the light curve """
        self._data.append((unix_time, magnitude, snr))

    def __len__(self):
        return len(self._data)

    def __getitem__(self, index):
        return self._data[index]

    def __iter__(self):
        """ Return a copy of the (unix_time, magnitude, snr) tuples,
        chronologically sorted"""
        return iter(sorted(self._data, key = operator.itemgetter(0)))

    @property
    def stdev(self):
        if not self:
            raise ValueError("light curve is empty")
        magnitudes = [mag for unix_time, mag, snr in self._data]
        return numpy.std(numpy.array(magnitudes, dtype = self.dtype))

    def weights(self):
        """ Return a generator over the pairs of comparison stars and their
            corresponding weights """
        for cstar_id, cweight in zip(self.cstars, self.cweights):
            yield cstar_id, cweight

    def amplitude(self, npoints = 1, median = True):
        """ Compute the peak-to-peak amplitude of the light curve.

        The amplitude of a light curve is usually defined, and in this manner
        it is calculated by default, as the change between peak (the highest
        value) and trough (lowest value). However, this method also allows to
        take into account that there might be outliers, caused by measurement
        errors, that could severely affect the result of this difference. Thus,
        it its possible to take the mean or median of several points as the
        peak and trough used to compute the amplitude. The ValueError exception
        is raised if there are no points in the light cuve (i.e., it is empty).

        Keyword arguments:
        npoints - the number of maximum and minimum points (i.e., differential
                  magnitudes) that are combined to obtain the peak and trough.
        median - whether the maximum and minimum points are combined taking
                 their median (if the parameter evaluates to True) or their
                 arithmetic mean (otherwise).

        """

        if not self:
            raise ValueError("light curve is empty")

        magnitudes = sorted(mag for unix_time, mag, snr in self._data)
        func = numpy.median if median else numpy.mean
        return func(magnitudes[-npoints:]) - func(magnitudes[:npoints])

    def ignore_noisy(self, snr):
        """ Return a copy of the LightCurve without noisy points.

        The method returns a deep copy of the instance from which those
        differential magnitudes whose signal-to-noise ratio is below 'snr'
        have been removed.

        """

        curve = copy.deepcopy(self)
        # _data stores three-element tuples: (Unix time, magnitude, snr)
        curve._data = [x for x in curve._data if x[-1] >= snr]
        return curve


class DuplicateImageError(KeyError):
    """ Raised if two Images with the same Unix time are added to a LEMONdB """
    pass

class DuplicateStarError(KeyError):
    """ Raised if tho stars with the same ID are added to a LEMONdB """
    pass

class UnknownStarError(sqlite3.IntegrityError):
    """ Raised when a star foreign key constraint fails """
    pass

class UnknownImageError(sqlite3.IntegrityError):
    """ Raised when an image foreign key constraint fails """
    pass

class DuplicatePeriodError(sqlite3.IntegrityError):
    """ Raised if more than one period for the same star and filter is added"""
    pass

class DuplicatePhotometryError(sqlite3.IntegrityError):
    """ Raised of more than one record for the same star and image is added"""
    pass

class DuplicateLightCurvePointError(sqlite3.IntegrityError):
    """ If more than one curve point for the same star and image is added"""
    pass

class LEMONdB(object):
    """ Interface to the SQLite database used to store our results """

    # Keys of the records stored in the METADATA tables
    _METADATA_DATE_KEY = 'DATE'     # date of creation of the LEMONdB
    _METADATA_AUTHOR_KEY = 'AUTHOR' # who ran LEMON to create the LEMONdB
    _METADATA_HOSTNAME_KEY = 'HOST' # where the LEMONdB was created
    _METADATA_ID_KEY = 'ID'         # unique identifier of the LEMONdB

    def __init__(self, path, dtype = numpy.longdouble):

        self.path = path
        self.dtype = dtype
        self.connection = sqlite3.connect(self.path, isolation_level = None)
        self._cursor = self.connection.cursor()

        # Enable foreign key support (SQLite >= 3.6.19)
        self._execute("PRAGMA foreign_keys = ON")
        self._execute("PRAGMA foreign_keys")
        if not self._rows.fetchone()[0]:
            raise sqlite3.NotSupportedError("foreign key support is not enabled")

        self._start()
        self._create_tables()
        self.commit()

    def __del__(self):
        self._cursor.close()
        self.connection.close()

    def _execute(self, query, t = ()):
        """ Execute SQL query; returns nothing """
        self._cursor.execute(query, t)

    @property
    def _rows(self):
        """ Return an iterator over the rows returned by the last query """
        return self._cursor

    def _start(self):
        """ Start a new transaction """
        self._execute("BEGIN TRANSACTION")

    def _end(self):
        """ End the current transaction """
        self._execute("END TRANSACTION")

    def commit(self):
        """ Make the changes of the current transaction permanent.
        Automatically starts a new transaction """
        self._end()
        self._start()

    def _savepoint(self, name = None):
        """ Start a new savepoint, use a random name if not given any.
        Returns the name of the savepoint that was started. """

        if not name:
            name = ''.join(random.sample(string.letters, 12))
        self._execute("SAVEPOINT %s" % name)
        return name

    def _rollback_to(self, name):
        """ Revert the state of the database to a savepoint """
        self._execute("ROLLBACK TO %s" % name)

    def _release(self, name):
        """ Remove from the transaction stack all savepoints back to and
        including the most recent savepoint with this name """
        self._execute("RELEASE %s" % name)

    def analyze(self):
        """ Run the ANALYZE command and commit automatically.

        This command gathers statistics about tables and indexes and stores the
        collected information in internal tables of the database where the
        query optimizer can access the information and use it to help make
        better query planning choices. These statistics are not automatically
        updated as the content of the database changes. If the content of the
        database changes significantly, or if the database schema changes, then
        one should consider rerunning the ANALYZE command in order to update
        the statistics. [https://www.sqlite.org/lang_analyze.html]

        """

        self._execute("ANALYZE")
        self.commit()

    def _create_tables(self):
        """ Create, if needed, the tables used by the database """

        # This table will contain non-relational information about the LEMONdB
        # itself: we need to store records (key-value pairs, such as ('AUTHOR',
        # 'John Doe'), and there cannot be more than one row for each key.
        self._execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE (key))
        ''')

        self._execute('''
        CREATE TABLE IF NOT EXISTS stars (
            id   INTEGER PRIMARY KEY,
            x    REAL NOT NULL,
            y    REAL NOT NULL,
            ra   REAL,
            dec  REAL,
            imag REAL NOT NULL)
        ''')

        self._execute('''
        CREATE TABLE IF NOT EXISTS photometric_filters (
            id    INTEGER PRIMARY KEY,
            name  TEXT UNIQUE NOT NULL)
        ''')

        self._execute('''
        CREATE TABLE IF NOT EXISTS photometric_parameters (
            id       INTEGER PRIMARY KEY,
            aperture INTEGER NOT NULL,
            annulus  INTEGER NOT NULL,
            dannulus INTEGER NOT NULL)
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS phot_params_all_rows "
                      "ON photometric_parameters(aperture, annulus, dannulus)")

        # Map (1) a set of photometric parameters and (2) a photometric filter
        # to a standard deviation. This table is populated by the photometry
        # module when the --annuli option is used, storing here the contents
        # of the XML file with all the candidate photometric parameters.

        self._execute('''
        CREATE TABLE IF NOT EXISTS candidate_parameters (
            id         INTEGER PRIMARY KEY,
            pparams_id INTEGER NOT NULL,
            filter_id  INTEGER NOT NULL,
            stdev      REAL NOT NULL,
            FOREIGN KEY (pparams_id) REFERENCES photometric_parameters(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            UNIQUE (pparams_id, filter_id))
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS cand_filter "
                      "ON candidate_parameters(filter_id)")

        # IMAGES table: the 'sources' column stores Boolean values as integers
        # 0 (False) and 1 (True), indicating the FITS image on which sources
        # were detected. Only one image must have 'sources' set to True; all
        # the others must be False.

        self._execute('''
        CREATE TABLE IF NOT EXISTS images (
            id         INTEGER PRIMARY KEY,
            path       TEXT NOT NULL,
            filter_id  INTEGER NOT NULL,
            unix_time  REAL NOT NULL,
            object     TEXT,
            airmass    REAL NOT NULL,
            gain       REAL NOT NULL,
            ra         REAL NOT NULL,
            dec        REAL NOT NULL,
            sources    INTEGER NOT NULL,
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            UNIQUE (filter_id, unix_time))

        ''')

        self._execute("CREATE INDEX IF NOT EXISTS img_by_filter_time "
                      "ON images(filter_id, unix_time)")

        # Store as a blob entire FITS files.
        self._execute('''
        CREATE TABLE IF NOT EXISTS raw_images (
            id   INTEGER PRIMARY KEY,
            fits BLOB NOT NULL,
            FOREIGN KEY (id) REFERENCES images(id))
        ''')

        self._execute('''
        CREATE TABLE IF NOT EXISTS photometry (
            id         INTEGER PRIMARY KEY,
            star_id    INTEGER NOT NULL,
            image_id   INTEGER NOT NULL,
            magnitude  REAL NOT NULL,
            snr        REAL NOT NULL,
            FOREIGN KEY (star_id)  REFERENCES stars(id),
            FOREIGN KEY (image_id) REFERENCES images(id),
            UNIQUE (star_id, image_id))
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS phot_by_star_image "
                      "ON photometry(star_id, image_id)")
        self._execute("CREATE INDEX IF NOT EXISTS phot_by_image "
                      "ON photometry(image_id)")

        self._execute('''
        CREATE TABLE IF NOT EXISTS light_curves (
            id         INTEGER PRIMARY KEY,
            star_id    INTEGER NOT NULL,
            image_id   INTEGER NOT NULL,
            magnitude  REAL NOT NULL,
            snr        REAL,
            FOREIGN KEY (star_id)  REFERENCES stars(id),
            FOREIGN KEY (image_id) REFERENCES images(id),
            UNIQUE (star_id, image_id))
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS curve_by_star_image "
                      "ON light_curves(star_id, image_id)")

        self._execute('''
        CREATE TABLE IF NOT EXISTS cmp_stars (
            id        INTEGER PRIMARY KEY,
            star_id   INTEGER NOT NULL,
            filter_id INTEGER NOT NULL,
            cstar_id  INTEGER NOT NULL,
            weight    REAL NOT NULL,
            FOREIGN KEY (star_id)    REFERENCES stars(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            FOREIGN KEY (cstar_id)   REFERENCES stars(id))
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS cstars_by_star_filter "
                      "ON cmp_stars(star_id, filter_id)")

        self._execute('''
        CREATE TABLE IF NOT EXISTS periods (
            id        INTEGER PRIMARY KEY,
            star_id   INTEGER NOT NULL,
            filter_id INTEGER NOT NULL,
            step      REAL NOT NULL,
            period    REAL NOT NULL,
            FOREIGN KEY (star_id)    REFERENCES stars(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            UNIQUE (star_id, filter_id))
        ''')

        self._execute("CREATE INDEX IF NOT EXISTS period_by_star_filter "
                      "ON periods(star_id, filter_id)")

    def _table_count(self, table):
        """ Return the number of rows in 'table' """
        self._execute("SELECT COUNT(*) FROM %s" % table)
        rows = list(self._rows) # from iterator to list
        assert len(rows) == 1
        return rows[0][0]

    def _add_pfilter(self, pfilter):
        """ Store a photometric filter in the database. The primary
        key of the Passband objects in the table is their hash value """

        t = (hash(pfilter), str(pfilter))
        self._execute("INSERT OR IGNORE INTO photometric_filters VALUES (?, ?)", t)

    @property
    def _pparams_ids(self):
        """ Return the ID of the photometric parameters, in ascending order"""
        self._execute("SELECT id "
                      "FROM photometric_parameters "
                      "ORDER BY id ASC")
        return list(x[0] for x in self._rows)

    def _get_pparams(self, id_):
        """ Return the PhotometricParamaters with this ID.
        Raises KeyError if the database has nothing for this ID """

        self._execute("SELECT aperture, annulus, dannulus "
                      "FROM photometric_parameters "
                      "WHERE id = ?", (id_,))
        rows = list(self._rows)
        if not rows:
            raise KeyError('%d' % id_)
        else:
            assert len(rows) == 1
            args = rows[0]
            return PhotometricParameters(*args)

    def _add_pparams(self, pparams):
        """ Add a PhotometricParameters instance and return its ID or do
        nothing and simply return the ID if already present in the database"""

        t = [pparams.aperture, pparams.annulus, pparams.dannulus]
        self._execute("SELECT id "
                      "FROM photometric_parameters "
                      "     INDEXED BY phot_params_all_rows "
                      "WHERE aperture = ? "
                      "  AND annulus  = ? "
                      "  AND dannulus = ?", t)
        try:
            return list(self._rows)[0][0]
        except IndexError:
            t.insert(0, None)
            self._execute("INSERT INTO photometric_parameters VALUES (?, ?, ?, ?)", t)
            return self._cursor.lastrowid

    def add_candidate_pparams(self, candidate_annuli, pfilter):
        """ Store a CandidateAnnuli instance into the LEMONdB.

        The method links an xmlparse.CandidateAnnuli instance to a photometric
        filter, adding a new record to the CANDIDATE_PARAMETERS table. This
        allows us to store in the LEMONdB the photometric parameters what were
        evaluated for each photometric filter, and how good they were (the
        lower the standard deviation, the better). Please refer to the docs of
        the xmlparse.CandidateAnnuli class and the annuli module for further
        information on how the optimal parameters for aperture photometry are
        identified.

        Adding, for the same filter, a CandidateAnnuli with the same aperture,
        annulus and dannulus (sky annulus) that a previously added object, but
        a different stdev, will replace the CandidateAnnuli already in the
        database. For example, if we are working with Johnson I and first add
        CandidateAnnuli(1.618, 14.885, 3.236, 0.476) and, later on,
        CandidateAnnuli(1.618, 14.885, 3.236, 0.981), also for Johnson I, the
        former record (that with stdev 0.981) will be replaced by the latter.

        """

        pparams_id = self._add_pparams(candidate_annuli)
        self._add_pfilter(pfilter)
        t = (None, pparams_id, hash(pfilter), candidate_annuli.stdev)
        self._execute("INSERT OR REPLACE INTO candidate_parameters "
                      "VALUES (?, ?, ?, ?)", t)

    def get_candidate_pparams(self, pfilter):
        """ Return all the CandidateAnnuli for a photometric filter.

        The method returns a list with all the CandidateAnnuli objects that
        have been stored in the LEMONdB (in the CANDIDATE_PARAMETERS table,
        using the add_candidate_pparams method) for the 'filter' photometric
        filter. The returned CandidateAnnuli are sorted in increasing order by
        their standard deviation; that is, the one with the lowest stdev goes
        first, while that with the highest stdev is the last one.

        """

        t = (hash(pfilter),)
        self._execute("SELECT p.aperture, p.annulus, p.dannulus, c.stdev "
                      " FROM candidate_parameters AS c "
                      "      INDEXED BY cand_filter, "
                      "      photometric_parameters AS p "
                      "ON c.pparams_id = p.id "
                      "WHERE c.filter_id = ? "
                      "ORDER BY c.stdev ASC", t)
        return [xmlparse.CandidateAnnuli(*args) for args in self._rows]

    def _get_simage_id(self):
        """ Return the ID of the image on which sources were detected.

        Return the ID of the FITS file that was used to detect sources: it can
        be identified in the IMAGES table because it is the only row where the
        value of the SOURCES column is equal to one. Raises KeyError if the
        sources image (LEMONdB.simage) has not yet been set.

        """

        self._execute("SELECT id FROM images WHERE sources = 1")
        rows = list(self._rows)
        if not rows:
            msg = "sources image has not yet been set"
            raise KeyError(msg)
        else:
            assert len(rows) == 1
            return rows[0][0]

    @property
    def simage(self):
        """ Return the FITS image on which sources were detected.

        Return an Image object with the information about the FITS file that
        was used to detect sources. The Image.path attribute is just that, a
        path, but a copy of the FITS image is also stored in the database, and
        is available through the LEMONdB.mosaic attribute. Returns None if the
        sources image has not yet been set.

        """

        try:
            id_ = self._get_simage_id()
        except KeyError:
            return None

        t = (id_,)
        self._execute("SELECT i.path, p.name, i.unix_time, i.object, "
                      "       i.airmass, i.gain, i.ra, i.dec "
                      "FROM images AS i, photometric_filters AS p "
                      "ON i.filter_id = p.id "
                      "WHERE i.id = ?", t)

        rows = list(self._rows)
        assert len(rows) == 1
        args = list(rows[0])
        args[1] = passband.Passband(args[1])
        return Image(*args)

    @simage.setter
    def simage(self, image):
        """ Set the FITS image on which sources were detected.

        Receives an Image object with information about the FITS file that was
        used to detect sources and stores it in the LEMONdB. The file to which
        Image.path refers must exist and be readable, as it is also stored in
        the database and accessible through the LEMON.mosaic attribute.

        """

        try:
            self.add_image(image)
        except DuplicateImageError:
            pass

        with open(image.path, 'rb') as fd:
            blob = fd.read()

        # The sources image is the only one with  SOURCES == 1
        id_ = self._get_image_id(image.unix_time, image.pfilter)
        self._execute("UPDATE images SET sources = 0")
        self._execute("UPDATE images SET sources = 1 WHERE id = ?", (id_,))
        t = (id_, buffer(blob))
        self._execute("INSERT OR REPLACE INTO raw_images VALUES (?, ?)", t)

    def add_image(self, image):
        """ Store information about a FITS image in the database.

        Raises DuplicateImageError if the Image has the same Unix time and
        photometric filter that another image already stored in the LEMON
        database (as these two values must be unique; i.e., we cannot have
        two or more images with the same Unix time and photometric filter).

        """

        # Use a SAVEPOINT to, if the insertion of the Image fails, be able
        # to roll back the insertion of the photometric filter.

        mark = self._savepoint()
        self._add_pfilter(image.pfilter)

        t = (None, image.path, hash(image.pfilter), image.unix_time,
             image.object, image.airmass, image.gain, image.ra, image.dec)
        try:
            self._execute("INSERT INTO images "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", t)
            self._release(mark)

        except sqlite3.IntegrityError:
            self._rollback_to(mark)
            unix_time = image.unix_time
            pfilter = image.pfilter

            if __debug__:
                self._execute("SELECT unix_time FROM images")
                assert (unix_time,) in self._rows
                self._execute("SELECT 1 "
                              "FROM photometric_filters "
                              "WHERE id = ?", (hash(pfilter),))
                assert [(1,)] == list(self._rows)

            msg = "Image with Unix time %.4f (%s) and filter %s already in database"
            args = (unix_time, methods.utctime(unix_time), pfilter)
            raise DuplicateImageError(msg % args)

    def _get_image_id(self, unix_time, pfilter):
        """ Return the ID of the Image with this Unix time and filter.
        Raises KeyError if there is no image for this date and filter"""

        # Note the cast to Python's built-in float. Otherwise, if the method
        # gets a NumPy float, SQLite raises "sqlite3.InterfaceError: Error
        # binding parameter - probably unsupported type"
        t = (float(unix_time), hash(pfilter))
        self._execute("SELECT id "
                      "FROM images INDEXED BY img_by_filter_time "
                      "WHERE unix_time = ? "
                      "  AND filter_id = ?", t)
        rows = list(self._rows)
        if not rows:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, methods.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        else:
            assert len(rows) == 1
            assert len(rows[0]) == 1
            return rows[0][0]

    def get_image(self, unix_time, pfilter):
        """ Return the Image observed at a Unix time and photometric filter.
        Raises KeyError if there is no image for this date and filter"""

        image_id = self._get_image_id(unix_time, pfilter)
        self._execute("SELECT i.path, p.name, i.unix_time, i.object, "
                      "       i.airmass, i.gain, i.ra, i.dec "
                      "FROM images AS i, photometric_filters AS p "
                      "ON i.filter_id = p.id "
                      "WHERE i.id = ?", (image_id,))

        rows = list(self._rows)
        if not rows:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, methods.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        else:
            assert len(rows) == 1
            args = list(rows[0])
            args[1] = passband.Passband(args[1])
            return Image(*args)

    def add_star(self, star_id, x, y, ra, dec, imag):
        """ Add a star to the database.

        This method only stores the 'description' of the star, that is, its
        image and celestial coordinates, as well as its instrumental magnitude
        in the reference image. To add the photometric records and the light
        curves, use LEMONdB.add_photometry and add_light_curve, respectively.
        Raises DuplicateStarError if the specified ID was already used for
        another star in the database.

        """

        t = (star_id, x, y, ra, dec, imag)
        try:
            self._execute("INSERT INTO stars VALUES (?, ?, ?, ?, ?, ?)", t)
        except sqlite3.IntegrityError:
            if __debug__:
                self._execute("SELECT id FROM stars")
                assert (star_id,) in self._rows
            msg = "star with ID = %d already in database" % star_id
            raise DuplicateStarError(msg)

    def get_star(self, star_id):
        """ Return the coordinates and magnitude of a star.

        The method returns a five-element tuple with, in this order: the x- and
        y- coordinates of the star in the reference image, the right ascension
        and declination and its instrumental magnitude in the reference image.
        Raises KeyError is no star in the database has this ID.

        """

        t = (star_id, )
        self._execute("SELECT x, y, ra, dec, imag "
                      "FROM stars "
                      "WHERE id = ?", t)
        try:
            return self._rows.next()
        except StopIteration:
            msg = "star with ID = %d not in database" % star_id
            raise KeyError(msg)

    def __len__(self):
        """ Return the number of stars in the database """
        return self._table_count('STARS')

    @property
    def star_ids(self):
        """ Return a list with the ID of the stars, in ascending order """
        self._execute("SELECT id FROM stars ORDER BY id ASC")
        return list(x[0] for x in self._rows)

    def add_photometry(self, star_id, unix_time, pfilter, magnitude, snr):
        """ Store the photometric record of a star at a given time and filter.

        Raises UnknownStarError if 'star_id' does not match the ID of any of
        the stars in the database, while UnknownImageError is raised if the
        Unix time and photometric filter do not match those of any of the
        images previously added. At most one photometric record can be stored
        for each star, image and photometric filter; therefore, the addition of
        a second record for the same star ID, Unix time and photometric filter
        causes DuplicatePhotometryError to be raised.

        """

        try:
            # Raises KeyError if no image has this Unix time and filter
            image_id = self._get_image_id(unix_time, pfilter)

            # Note the casts to Python's built-in float. Otherwise, if the
            # method gets a NumPy float, SQLite raises "sqlite3.InterfaceError:
            # Error binding parameter - probably unsupported type"
            t = (None, star_id, image_id, float(magnitude), float(snr))
            self._execute("INSERT INTO photometry VALUES (?, ?, ?, ?, ?)", t)

        except KeyError, e:
            raise UnknownImageError(str(e))

        except sqlite3.IntegrityError:
            if not star_id in self.star_ids:
                msg = "star with ID = %d not in database" % star_id
                raise UnknownStarError(msg)

            msg = "photometry for star ID = %d, Unix time = %4.f " \
                  "(%s) and filter %s already in database"
            args = (star_id, unix_time, methods.utctime(unix_time), pfilter)
            raise DuplicatePhotometryError(msg % args)

    def get_photometry(self, star_id, pfilter):
        """ Return the photometric information of the star.

        The method returns a DBStar instance with the photometric information
        of the star in a given filter. The records are sorted by their date of
        observation. Raises KeyError if 'star_id' does not match the ID of any
        of the stars in the database.

        """

        if star_id not in self.star_ids:
            msg = "star with ID = %d not in database" % star_id
            raise KeyError(msg)

        # Note the cast to Python's built-in int. Otherwise, if the method gets
        # a NumPy integer, SQLite raises "sqlite3.InterfaceError: Error binding
        # parameter - probably unsupported type"
        t = (int(star_id), hash(pfilter))
        self._execute("SELECT img.unix_time, phot.magnitude, phot.snr "
                      "FROM photometry AS phot INDEXED BY phot_by_star_image, "
                      "     images AS img INDEXED BY img_by_filter_time "
                      "ON phot.image_id = img.id "
                      "WHERE phot.star_id = ? "
                      "  AND img.filter_id = ? "
                      "ORDER BY img.unix_time ASC", t)

        args = star_id, pfilter, list(self._rows)
        return DBStar.make_star(*args, dtype = self.dtype)

    def _star_pfilters(self, star_id):
        """ Return the photometric filters for which the star has data.

        The method returns a sorted list of the photometric filters
        (encapsulated as Passband instances) of the images on which the star
        with this ID had photometry done. Raises KeyError is no star in the
        database has the specified ID.

        """

        if star_id not in self.star_ids:
            msg = "star with ID = %d not in database" % star_id
            raise KeyError(msg)

        t = (star_id, )
        self._execute("""SELECT DISTINCT f.name
                         FROM (SELECT DISTINCT image_id
                               FROM photometry INDEXED BY phot_by_star_image
                               WHERE star_id = ?) AS phot
                         INNER JOIN images AS img
                         ON phot.image_id = img.id
                         INNER JOIN photometric_filters AS f
                         ON img.filter_id = f.id """, t)

        return sorted(passband.Passband(x[0]) for x in self._rows)

    @property
    def pfilters(self):
        """ Return the photometric filters for which there is data.

        The method returns a sorted list of the photometric filters for which
        the database has photometric records. Note that this means that a
        filter for which there are images (LEMONdB.add_image) but no
        photometric records (those added with LEMONdB.add_photometry) will not
        be included in the returned list.

        The photometric filter of the reference image is ignored. This
        means that if, say, it was observed in the Johnson I filter while the
        rest of the images of the campaign were taken in Johnson B, only the
        latter will be returned.

        """

        self._execute("""SELECT DISTINCT f.name
                         FROM (SELECT DISTINCT image_id
                               FROM photometry INDEXED BY phot_by_image)
                               AS phot
                         INNER JOIN images AS img
                         ON phot.image_id = img.id
                         INNER JOIN photometric_filters AS f
                         ON img.filter_id = f.id """)

        return sorted(passband.Passband(x[0]) for x in self._rows)

    def _add_curve_point(self, star_id, unix_time, pfilter, magnitude, snr):
        """ Store a point of the light curve of a star.

        Raises UnknownStarError if 'star_id' does not match the ID of any of
        the stars in the database, while UnknownImageError is raised if the
        Unix time and photometric filter do not match those of any of the
        images previously added. At most one light curve point can be stored
        for each star and image, so the addition of a second point for the same
        star ID, Unix time and filter causes DuplicateLightCurvePointError to
        be raised.

        """

        try:
            # Raises KeyError if no image has this Unix time and filter
            image_id = self._get_image_id(unix_time, pfilter)

            # Note the casts to Python's built-in float. Otherwise, if the
            # method gets a NumPy float, SQLite raises "sqlite3.InterfaceError:
            # Error binding parameter - probably unsupported type"
            t = (None, star_id, image_id, float(magnitude), float(snr))
            self._execute("INSERT INTO light_curves "
                          "VALUES (?, ?, ?, ?, ?)", t)

        except KeyError, e:
            raise UnknownImageError(str(e))

        except sqlite3.IntegrityError:
            if not star_id in self.star_ids:
                msg = "star with ID = %d not in database" % star_id
                raise UnknownStarError(msg)

            msg = "light curve point for star ID = %d, Unix time = %4.f " \
                  "(%s) and filter %s already in database"
            args = (star_id, unix_time, methods.utctime(unix_time), pfilter)
            raise DuplicateLightCurvePointError(msg % args)

    def _add_cmp_star(self, star_id, pfilter, cstar_id, cweight):
        """ Add a comparison star to the light curve of a star.

        The method stores 'cstar_id' as the ID of one of the comparison stars,
        with a weight of 'cweight', that were used to compute the light curve
        of the star with ID 'star_id' in the 'pfilter' photometric filter.

        Raises UnknownStarError if either 'star_id' or 'cstar_id' do not match
        the ID of any of the stars in the database. Since a star cannot use
        itself as a comparison star, ValueError is thrown in case the value of
        'star_id' is equal to 'cstar_id'.

        """

        if star_id == cstar_id:
            msg = "star with ID = %d cannot use itself as comparison" % star_id
            raise ValueError(msg)

        mark = self._savepoint()
        try:
            self._add_pfilter(pfilter)
            # Note the cast to Python's built-in float. Otherwise, if the
            # method gets a NumPy float, SQLite raises "sqlite3.InterfaceError:
            # Error binding parameter - probably unsupported type"
            t = (None, star_id, hash(pfilter), cstar_id, float(cweight))
            self._execute("INSERT INTO cmp_stars "
                          "VALUES (?, ?, ?, ?, ?)", t)
            self._release(mark)

        except sqlite3.IntegrityError:
            self._rollback_to(mark)
            if star_id not in self.star_ids:
                msg = "star with ID = %d not in database" % star_id
                raise UnknownStarError(msg)
            else:
                msg = "comparison star with ID = %d not in database" % cstar_id
                raise UnknownStarError(msg)

    def add_light_curve(self, star_id, light_curve):
        """ Store the light curve of a star.

        The database is modified atomically, so in case an error is encountered
        it is left untouched. There are four different exceptions, propagated
        from the LEMONdB._add_curve_point and LEMONdB._add_cmp_star methods,
        that may be raised:

        (1) UnknownStarError if either the star or any of its comparison stars
        are not stored in the database. Thus, LEMONdB.add_star must have been
        used in advance to store the stars with these IDs.

        (2) UnknownImageError if any of the Unix times in the light curve does
        not match that of any of the images in the database with the same
        photometric filter. Therefore, before a light curve is stored, the
        Images to which its points refer must have been added with the
        LEMONdB.add_image method.

        (3) DuplicateLightCurvePointError if the light curve has more than one
        point for the same Unix time, or if the light curve of a star is added
        more than once.

        (4) ValueError if the star uses itself as one of its comparison stars.
        This means, in other words, that in no case can 'star_id' be among the
        IDs listed in the 'cstars' attribute of the light curve.

        """

        mark = self._savepoint()
        try:
            for unix_time, magnitude, snr in light_curve:
                args = star_id, unix_time, light_curve.pfilter, magnitude, snr
                self._add_curve_point(*args)
            for weight in light_curve.weights():
                self._add_cmp_star(star_id, light_curve.pfilter, *weight)
            self._release(mark)
        except:
            self._rollback_to(mark)
            raise

    def get_light_curve(self, star_id, pfilter):
        """ Return the light curve of a star.

        The method returns a LightCurve instance which encapsulates the
        differential photometry of the star in a photometric filter. Raises
        KeyError is no star in the database has the specified ID, while, if
        the star exists but has no light curve in this photometric filter,
        None is returned.

        Although you should never come across it, sqlite3.IntegrityError is
        raised in case of data corruption, namely if the curve does not have
        any comparison stars. As you might remember, each curve (and this is
        enforced by the LightCurve class) requires of at least one comparison
        star; otherwise they could have never been stored in the database.

        """

        # String common across all error messages
        err_msg = "star with ID = %d " % star_id

        # Extract the points of the light curve ...
        t = (star_id, hash(pfilter))
        self._execute("SELECT img.unix_time, curve.magnitude, curve.snr "
                      "FROM light_curves AS curve INDEXED BY curve_by_star_image, "
                      "     images AS img INDEXED BY img_by_filter_time "
                      "ON curve.image_id = img.id "
                      "WHERE curve.star_id = ? "
                      "  AND img.filter_id = ? "
                      "ORDER BY img.unix_time ASC", t)
        curve_points = list(self._rows)

        if curve_points:
            # ... as well as the comparison stars.
            self._execute("SELECT cstar_id, weight "
                          "FROM cmp_stars INDEXED BY cstars_by_star_filter "
                          "WHERE star_id = ? "
                          "  AND filter_id = ? "
                          "ORDER BY cstar_id", t)

            rows = list(self._rows)
            if not rows:
                # This should never happen -- see docstring
                msg = err_msg + "has no comparison stars (?) in %s" % pfilter
                raise sqlite3.IntegrityError(msg)
            else:
                cstars, cweights = zip(*rows)

        else:
            if star_id not in self.star_ids:
                msg = err_msg + "not in database"
                raise KeyError(msg)

            # No curve in the database for this star and filter
            return None

        curve = LightCurve(pfilter, cstars, cweights, dtype = self.dtype)
        for point in curve_points:
            curve.add(*point)
        return curve

    def add_period(self, star_id, pfilter, period, step):
        """ Store the string-length period of a star.

        Add to the database the period of the star, computed using Dworetsky's
        string-length method (http://adsabs.harvard.edu/abs/1983MNRAS.203..917D)
        with a step of 'step' seconds.

        Raises UnknownStarError if 'star_id' does not match the ID of any of
        the stars in the database, and DuplicatePeriodError if the period of
        this star in this photometric filter is already in the database. As the
        filter may have to be added, the database is modified atomically, so it
        is guaranteed to be left untouched in case an error is encountered.

        """

        mark = self._savepoint()
        try:
            self._add_pfilter(pfilter)
            # Note the casts to Python's built-in float. Otherwise, if the
            # method gets a NumPy float, SQLite raises "sqlite3.InterfaceError:
            # Error binding parameter - probably unsupported type"
            t = (None, star_id, hash(pfilter), float(step), float(period))
            self._execute("INSERT INTO periods "
                          "VALUES (?, ?, ?, ?, ?)", t)
            self._release(mark)

        except sqlite3.IntegrityError:
            self._rollback_to(mark)
            if not star_id in self.star_ids:
                msg = "star with ID = %d not in database" % star_id
                raise UnknownStarError(msg)
            else:
                msg = "period for star ID = %d and photometric filter " \
                      "%s already in database" % (star_id, pfilter)
            raise DuplicatePeriodError(msg)

    def get_period(self, star_id, pfilter):
        """ Return the period of a star.

        The method returns a two-element tuple with the string-length period of
        the star in a photometric filter and the step that was used to find it.
        Both values are expressed in seconds. Raises KeyError is no star has
        the specified ID, while, if the star exists but its period in this
        photometric filter is not stored in the database, None is returned.

        """

        t = (star_id, hash(pfilter))
        self._execute("SELECT period, step "
                      "FROM periods INDEXED BY period_by_star_filter "
                      "WHERE star_id = ? "
                      "  AND filter_id = ?", t)
        try:
            rows = tuple(self._rows)
            return rows[0]
        except IndexError:
            if star_id not in self.star_ids:
                msg = "star with ID = %d not in database" % star_id
                raise KeyError(msg)
            else:
                return None

    def get_periods(self, star_id):
        """ Return all the periods of a star.

        Return a NumPy array with the string-length periods of the star in all
        the photometric filters for which they are known. This is a convenience
        function to retrieve the periods of the star (in order to, for example,
        examine how similar they are) without having to call LEMONdB.get_period
        star multiple times. Raises KeyError is no star has the specified ID

        In case no period of the star is known, an empty array is returned. The
        periods may be returned in any order, so there is no way of knowing to
        which photometric filter each one correspond. Use LEMONdB.get_period
        instead if you need to know what the period is in a specific filter.

        """

        t = (star_id,)
        self._execute("SELECT period "
                      "FROM periods INDEXED BY period_by_star_filter "
                      "WHERE star_id = ? ", t)
        periods = tuple(x[0] for x in self._rows)
        if not periods and star_id not in self.star_ids:
            msg = "star with ID = %d not in database" % star_id
            raise KeyError(msg)
        else:
            return numpy.array(periods)

    def airmasses(self, pfilter):
        """ Return the airmasses of the images in a photometric filter.

        The method returns a dictionary which maps the Unix time of each of the
        images in this photometric filter to their airmasses. The airmass of
        the reference image is irrelevant, as photometry is not done on it, so
        it is not considered and never included in the returned dictionary. If
        no images were taken in this filter, an empty dictionary is returned.

        """

        t = (hash(pfilter), )
        self._execute("SELECT unix_time, airmass "
                      "FROM images INDEXED BY img_by_filter_time "
                      "WHERE filter_id = ? ", t)
        return dict(self._rows)

    def get_phase_diagram(self, star_id, pfilter, period, repeat = 1):
        """ Return the folded light curve of a star.

        The method returns a LightCurve instance with the phase diagram of the
        star in a photometric filter: 'Phase diagrams (also known as 'folded
        light curves') are a useful tool for studying the behavior of periodic
        stars such as Cepheid variables and eclipsing binaries. In a phase
        diagram, multiple cycles of brightness variation are superimposed on
        each other. Instead of plotting magnitude versus Julian date as with a
        regular light curve, each observation is plotted as a function of 'how
        far into the cycle' it is' [http://www.aavso.org/data/lcg/curve.shtml].

        The 'repeat' keyword argument determines how many times the cycle is
        repeated, in order help us more easily appreciate what the shape of the
        period is. A 'phased Unix time' of 0,05, for example, becomes 1.05 the
        first time the phase diagram is repeated, 2.05 the second time, etc.

        Raises KeyError is no star in the database has the specified ID, while,
        if the star exists but has no light curve in this photometric filter,
        None is returned.

        """

        curve = self.get_light_curve(star_id, pfilter)
        if curve is None:
            return None

        phase = LightCurve(pfilter, curve.cstars,
                           curve.cweights, dtype = curve.dtype)
        unix_times, magnitudes, snrs = zip(*curve)
        zero_t = min(unix_times)

        phased_x = []
        for utime, mag, snr in zip(unix_times, magnitudes, snrs):
            # How far into the cycle is this Unix time?
            fractional_part = math.modf((utime - zero_t) / period)[0]
            phased_x.append(fractional_part)
        assert len(phased_x) == len(unix_times)

        x_max = 1;
        phased_unix_times = phased_x[:]
        for _ in xrange(repeat - 1): # -1 as there is already one (phased_x)
            phased_unix_times += [utime + x_max for utime in phased_x]
            x_max += 1;

        assert len(phased_unix_times) == len(unix_times) * repeat
        phased_magnitudes = magnitudes * repeat
        phased_snr = snrs * repeat

        for utime, mag, snr in \
            zip(phased_unix_times, phased_magnitudes, phased_snr):
                phase.add(utime, mag, snr)

        assert len(phase) == len(curve) * repeat
        return phase

    def most_similar_magnitude(self, star_id, pfilter):
        """ Iterate over the stars sorted by their similarity in magnitude.

        Returns a generator over the stars in the LEMONdB that have a light
        curve in the 'pfilter' photometric filter, sorted by the difference
        between their instrumental magnitudes and that of the star with ID
        'star_id'. In other words: the first returned star will be that whose
        instrumental magnitude is most similar to that of 'star_id', while the
        last one will be that with the most different magnitude. At each step,
        a two-element tuple with the ID of the star and its instrumental
        magnitude is returned.

        """

        # Map each ID other than 'star_id' to its instrumental magnitude
        magnitudes = [(id_, self.get_star(id_)[-1])
                      for id_ in self.star_ids if id_ != star_id]

        # Sort the IDs by the difference between their instrumental magnitude
        # and the reference instrumental magnitude (that of the star with ID
        # 'star_id', and return one by one those which have a light curve
        rmag = self.get_star(star_id)[-1]
        magnitudes.sort(key = lambda x: abs(rmag - x[1]))
        for id_, imag in magnitudes:
            if self.get_light_curve(id_, pfilter):
                yield id_, imag

    @property
    def field_name(self):
        """ Determine the name of the field observed during a campaign.

        The method finds the most common prefix among the object names of the
        images (IMAGES table) contained in the database. What we understand by
        'most common prefix' in this context is the longest substring with
        which more than half of the images start. The purpose of this method is
        to allow to automatically determine which field was observed, without
        relying on a single image but instead by analyzing all of them. The
        ValueError exception is raised if there are no images in the LEMONdB.
        None is returned, on the other hand, if there is no prefix common to
        the object names (e.g., if there are two images whose names are
        'FT_Tau_1minB' and 'BD+78_779_20minV').

        For example: if there are only three images in the LEMONdB and their
        object names are 'IC5146_30minV', 'IC5146_30minR' and 'IC5146_1minI',
        the string 'IC5146' is returned. Trailing whitespaces and underscores
        are stripped from the common prefix.

        """

        self._execute("SELECT object FROM images")
        object_names = [x[0] for x in self._rows]

        if not object_names:
            msg = "database contains no images"
            raise ValueError(msg)

        # Loop over all the object names stored in the LEMONdB, keeping the
        # track of how many times each prefix is seen (e.g., 'abc' gives us
        # three different prefixes: 'a', 'ab' and 'abc').
        substrings = collections.defaultdict(int)
        for name in object_names:
            for index in range(1, len(name) + 1):
                substrings[name[:index]] += 1

        def startswith_counter(prefix, names):
            """ Return the number of strings in 'names' starting with 'prefix' """
            return len([x for x in names if x.startswith(prefix)])

        # Loop over the prefixes, from longest to shortest, until one common to
        # more than half of the object names (i.e., images) in the database is
        # found.
        longest = sorted(substrings.keys(), key = len, reverse = True)
        minimum_matches = len(object_names) // 2 + 1
        for prefix in longest:
            if startswith_counter(prefix, object_names) >= minimum_matches:
                return prefix.strip(" _") # e.g., "NGC 2276_" to "NGC 2264"

    def _set_metadata(self, key, value):
        """ Set (or replace) the value of a record in the METADATA table.

        Both the key and the value are cast to string and cannot be NULL, and
        therefore the ValueError exception will be raised if None is used. Note
        that empty strings are allowed, however (but why would you do that?)"""

        if key is None:
            raise ValueError("key cannot be None")
        if value is None:
            raise ValueError("value cannot be None")

        t = (str(key), str(value))
        self._execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", t)

    def _get_metadata(self, key):
        """ Return the value of a record in the METADATA table. None is
        returned if 'key' does not match that of any key-value pair. """

        t = (key, )
        self._execute("SELECT value FROM metadata WHERE key = ?", t)
        rows = tuple(self._rows)
        if not rows:
            return None
        else:
            assert len(rows) == 1
            assert len(rows[0]) == 1
            return rows[0][0]

    def _get_date(self):
        """ Return the date of creation of the LEMONdB, cast to float"""
        value = self._get_metadata(self._METADATA_DATE_KEY)
        if value is not None:
            value = float(value)
        return value

    def _set_date(self, unix_time):
        """ Set (or replace) the date of creation of the LEMONdB """
        self._set_metadata(self._METADATA_DATE_KEY, unix_time)

    date = property(_get_date, _set_date)

    def _get_author(self):
        """ Return the name of the user who created the LEMONdB """
        return self._get_metadata(self._METADATA_AUTHOR_KEY)

    def _set_author(self, author):
        """ Set (or replace) the name of the user who created the LEMONdB """
        self._set_metadata(self._METADATA_AUTHOR_KEY, author)

    author = property(_get_author, _set_author)

    def _get_hostname(self):
        """ Return the hostname of the machine where the LEMONdB was created"""
        return self._get_metadata(self._METADATA_HOSTNAME_KEY)

    def _set_hostname(self, host):
        """ Set / replace the hostname of the machine the LEMONdB was created"""
        self._set_metadata(self._METADATA_HOSTNAME_KEY, host)

    hostname = property(_get_hostname, _set_hostname)

    def _get_id(self):
        """ Return the unique identifier of the LEMONdB """
        return self._get_metadata(self._METADATA_ID_KEY)

    def _set_id(self, id_):
        """ Set (or replace) the unique identifier of the LEMONdB """
        self._set_metadata(self._METADATA_ID_KEY, id_)

    id = property(_get_id, _set_id)

    @property
    def mosaic(self):
        """ Save to disk the FITS file used as reference frame.

        The method saves to disk the FITS file, stored as a blob in the
        database, that was used as a reference frame. The file is copied to a
        temporary location with the '.fits' extension. Returns the path to the
        FITS file, or None if the reference frame has no FITS file associated.
        It is important to note that the FITS file is saved to a *different*
        temporary location every time this method is called, so accessing the
        LEMONdB.mosaic attribute multiple times means that the same number of
        copies of the file will be copied to disk.

        """

        self._execute("SELECT fits FROM raw_images WHERE id = 0")
        rows = list(self._rows)
        if not rows:
            return None
        else:
            assert len(rows) == 1
            assert len(rows[0]) == 1
            blob = rows[0][0]
            fd, path = tempfile.mkstemp(suffix = '.fits')
            os.write(fd, blob)
            os.close(fd)
            return path

    @mosaic.setter
    def mosaic(self, path):
        """ Insert in the LEMONdB the FITS file of the reference frame """

        with open(path, 'rb') as fd:
            blob = fd.read()
        t = (0, buffer(blob))
        self._execute("DELETE FROM raw_images WHERE id = 0")
        self._execute("INSERT INTO raw_images VALUES (?, ?)", t)

    def star_closest_to_image_coords(self, x, y):
        """ Find the star closest to the image x- and y-coordinates.

        Compute the Euclidean distance from the x- and y-coordinates of each
        star in the LEMONdB to the coordinates (x, y). Returns a two-element
        tuple containing the ID of the closest star to these coordinates and
        its Euclidean distance, respectively. Raises ValueError if there are
        no stars in the LEMONdB when this method is called.

        """

        if not len(self):
            raise ValueError("database is empty")

        self._execute("SELECT id, x, y FROM stars")

        closest_id = None
        closest_distance = float('inf')
        for star_id, star_x, star_y in self._rows:
            star_distance = math.sqrt((star_x - x) ** 2 + (star_y - y) ** 2)
            if star_distance < closest_distance:
                closest_id = star_id
                closest_distance = star_distance

        return closest_id, closest_distance

