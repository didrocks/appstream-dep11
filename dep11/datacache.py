#!/usr/bin/env python3
#
# Copyright (C) 2014-2015 Matthias Klumpp <mak@debian.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.

import os
import logging as log
import lmdb


def tobytes(s):
    if isinstance(s, bytes):
        return s
    return bytes(s, 'utf-8')

class DataCache:
    """ A LMDB based cache for the DEP-11 generator """

    def __init__(self, media_dir):
        self._pkgdb = None
        self._hintsdb = None
        self._datadb = None
        self._opened = False

        self.media_dir = media_dir

    def open(self, cachedir):
        self._dbenv = lmdb.open(cachedir, max_dbs=3)

        self._pkgdb = self._dbenv.open_db(b'packages')
        self._hintsdb = self._dbenv.open_db(b'hints')
        self._datadb = self._dbenv.open_db(b'metadata')

        self._opened = True
        return True

    def close(self):
        self._dbenv.close()
        self._opened = False

    def has_metadata(self, global_id):
        gid = tobytes(global_id)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            return txn.get(gid) != None

    def get_metadata_for_pkg(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as pktxn:
            with self._dbenv.begin(db=self._datadb) as dtxn:

                cs_str = pktxn.get_str(pkgid)
                if not cs_str:
                    return None
                if cs_str == "ignore":
                    return None
                csl = cs_str.split("\n")

                data = ""
                for cs in csl:
                    d = dtxn.get_str(tobytes(cs))
                    if d:
                        data += str(d, 'utf-8')
                return data

    def set_metadata(self, global_id, yaml_data):
        gid = tobytes(global_id)
        with self._dbenv.begin(db=self._datadb, write=True) as txn:
            txn.put(gid, tobytes(yaml_data))

    def set_package_ignore(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb, write=True) as txn:
            txn.put(pkgid, b'ignore')

    def set_components(self, pkgid, cpts):
        # if the package has no components,
        # mark it as always-ignore
        if len(cpts) == 0:
            self.set_package_ignore(pkgid)
            return

        pkgid = tobytes(pkgid)

        gids = list()
        hints_str = ""
        for cpt in cpts:
            # get the metadata in YAML format
            md_yaml = cpt.to_yaml_doc()
            if not cpt.has_ignore_reason():
                if not self.has_metadata(cpt.global_id):
                    self.set_metadata(cpt.global_id, md_yaml)
                gids.append(cpt.global_id)
            hints_yml = cpt.get_hints_yaml()
            if hints_yml:
                hints_str += hints_yml

        self.set_hints(pkgid, hints_str)
        if gids:
            with self._dbenv.begin(db=self._pkgdb, write=True) as txn:
                txn.put(pkgid, bytes("\n".join(gids), 'utf-8'))

    def get_hints(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._hintsdb) as txn:
            hints = txn.get(pkgid)
            if hints:
                hints = str(hints, 'utf-8')
            return hints

    def set_hints(self, pkgid, hints_yml):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._hintsdb, write=True) as txn:
            txn.put(pkgid, tobytes(hints_yml))

    def remove_package(self, pkgid):
        with self._dbenv.begin(db=self._pkgdb, write=True) as pktxn:
            with self._dbenv.begin(db=self._hintsdb, write=True) as htxn:
                pktxn.delete(tobytes(pkgid))
                htxn.delete(tobytes(pkgid))

    def is_ignored(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            return txn.get(pkgid) == b'ignore'

    def package_exists(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            return txn.get(pkgid) != None
