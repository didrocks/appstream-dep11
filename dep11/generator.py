#!/usr/bin/env python3
#
# Copyright (C) 2015 Matthias Klumpp <mak@debian.org>
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
import sys
import yaml
import apt_pkg
import gzip
import tarfile
import glob
import shutil
import time
import traceback
from jinja2 import Environment, FileSystemLoader
from argparse import ArgumentParser
import multiprocessing as mp
import logging as log

from dep11 import MetadataExtractor, DataCache, build_cpt_global_id
from dep11.component import DEP11Component, get_dep11_header, dict_to_dep11_yaml
from dep11.iconfinder import ContentsListIconFinder
from dep11.utils import read_packages_dict_from_file
from dep11.hints import get_hint_tag_info
from dep11.validate import DEP11Validator

try:
    import pygments
    from pygments.lexers import YamlLexer
    from pygments.formatters import HtmlFormatter
except:
    pygments = None

def safe_move_file(old_fname, new_fname):
    if not os.path.isfile(old_fname):
        return
    if os.path.isfile(new_fname):
        os.remove(new_fname)
    os.rename(old_fname, new_fname)


def get_pkg_id(name, version, arch):
    return "%s/%s/%s" % (name, version, arch)


def equal_dicts(d1, d2, ignore_keys):
    ignored = set(ignore_keys)
    for k1, v1 in d1.items():
        if k1 not in ignored and (k1 not in d2 or d2[k1] != v1):
            return False
    for k2, v2 in d2.items():
        if k2 not in ignored and k2 not in d1:
            return False
    return True


def extract_metadata(mde, sn, pkgname, package_fname, version, arch, pkid):
    # we're now in a new process and can (re)open a LMDB connection
    mde.reopen_cache()
    cpts = mde.process(pkgname, package_fname, pkid)

    msgtxt = "Processed: %s (%s/%s), found %i" % (pkgname, sn, arch, len(cpts))
    return msgtxt


def load_generator_config(wdir):
    conf_fname = os.path.join(wdir, "dep11-config.yml")
    if not os.path.isfile(conf_fname):
        print("Could not find configuration! Make sure 'dep11-config.yml' exists!")
        return None

    f = open(conf_fname, 'r')
    conf = yaml.safe_load(f.read())
    f.close()

    if not conf:
        print("Configuration is empty!")
        return None

    if not conf.get("ArchiveRoot"):
        print("You need to specify an archive root path.")
        return None

    if not conf.get("Suites"):
        print("Config is missing information about suites!")
        return None

    if not conf.get("MediaBaseUrl"):
        print("You need to specify an URL where additional data (like screenshots) can be downloaded.")
        return None

    return conf


class DEP11Generator:
    def __init__(self):
        pass


    def initialize(self, dep11_dir):
        dep11_dir = os.path.abspath(dep11_dir)

        conf = load_generator_config(dep11_dir)
        if not conf:
            return False

        self._dep11_url = conf.get("MediaBaseUrl")
        self._icon_sizes = conf.get("IconSizes")
        if not self._icon_sizes:
            self._icon_sizes = ["128x128", "64x64"]

        self._archive_root = conf.get("ArchiveRoot")

        cache_dir = os.path.join(dep11_dir, "cache")
        if conf.get("CacheDir"):
            cache_dir = conf.get("CacheDir")

        self._export_dir = os.path.join(dep11_dir, "export")
        if conf.get("ExportDir"):
            self._export_dir = conf.get("ExportDir")

        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        if not os.path.exists(self._export_dir):
            os.makedirs(self._export_dir)

        self._suites_data = conf['Suites']

        self._distro_name = conf.get("DistroName")
        if not self._distro_name:
            self._distro_name = "Debian"

        # initialize our on-dik metadata pool
        self._cache = DataCache(self._get_media_dir())
        ret = self._cache.open(cache_dir)

        os.chdir(dep11_dir)
        return ret


    def _get_media_dir(self):
        mdir = os.path.join(self._export_dir, "media")
        if not os.path.exists(mdir):
            os.makedirs(mdir)
        return mdir


    def _get_packages_for(self, suite, component, arch):
        return read_packages_dict_from_file(self._archive_root, suite, component, arch).values()


    def make_icon_tar(self, suitename, component, pkglist):
        '''
         Generate icons-%(size).tar.gz
        '''
        dep11_mediadir = self._get_media_dir()
        names_seen = set()
        tar_location = os.path.join(self._export_dir, "data", suitename, component)

        size_tars = dict()

        for pkg in pkglist:
            pkid = get_pkg_id(pkg['name'], pkg['version'], pkg['arch'])

            gids = self._cache.get_cpt_gids_for_pkg(pkid)
            if not gids:
                # no component global-ids == no icons to add to the tarball
                continue

            for gid in gids:
                for size in self._icon_sizes:
                    icon_location_glob = os.path.join (dep11_mediadir, component, gid, "icons", size, "*.png")

                    tar = None
                    if size not in size_tars:
                        icon_tar_fname = os.path.join(tar_location, "icons-%s.tar.gz" % (size))
                        size_tars[size] = tarfile.open(icon_tar_fname+".new", "w:gz")
                    tar = size_tars[size]

                    for filename in glob.glob(icon_location_glob):
                        icon_name = os.path.basename(filename)
                        if size+"/"+icon_name in names_seen:
                            continue
                        tar.add(filename, arcname=icon_name)
                        names_seen.add(size+"/"+icon_name)

        for tar in size_tars.values():
            tar.close()
            # FIXME Ugly....
            safe_move_file(tar.name, tar.name.replace(".new", ""))


    def process_suite(self, suite_name):
        '''
        Extract new metadata for a given suite.
        '''

        suite = self._suites_data.get(suite_name)
        if not suite:
            log.error("Suite '%s' not found!" % (suite_name))
            return False

        dep11_mediadir = self._get_media_dir()

        # We need 'forkserver' as startup method to prevent deadlocks on join()
        # Something in the extractor is doing weird things, makes joining impossible
        # when using simple fork as startup method.
        mp.set_start_method('forkserver')

        for component in suite['components']:
            all_cpt_pkgs = list()
            for arch in suite['architectures']:
                pkglist = self._get_packages_for(suite_name, component, arch)

                # compile a list of packages that we need to look into
                pkgs_todo = dict()
                for pkg in pkglist:
                    pkid = get_pkg_id(pkg['name'], pkg['version'], pkg['arch'])

                    # check if we scanned the package already
                    if self._cache.package_exists(pkid):
                        continue
                    pkgs_todo[pkid] = pkg

                # set up metadata extractor
                iconf = ContentsListIconFinder(suite_name, component, arch, self._archive_root)
                mde = MetadataExtractor(suite_name,
                                component,
                                self._icon_sizes,
                                self._cache,
                                iconf)

                # Multiprocessing can't cope with LMDB open in the cache,
                # but instead of throwing an error or doing something else
                # that makes debugging easier, it just silently skips each
                # multprocessing task. Stupid thing.
                # (remember to re-open the cache later)
                self._cache.close()

                # set up multiprocessing
                with mp.Pool(maxtasksperchild=16) as pool:
                    def handle_results(message):
                        log.info(message)

                    def handle_error(e):
                        traceback.print_exception(type(e), e, e.__traceback__)
                        log.error(str(e))
                        pool.terminate()
                        sys.exit(5)

                    log.info("Processing %i packages in %s/%s/%s" % (len(pkgs_todo), suite_name, component, arch))
                    for pkid, pkg in pkgs_todo.items():
                        package_fname = os.path.join (self._archive_root, pkg['filename'])
                        if not os.path.exists(package_fname):
                            log.warning('Package not found: %s' % (package_fname))
                            continue
                        pool.apply_async(extract_metadata,
                                    (mde, suite_name, pkg['name'], package_fname, pkg['version'], pkg['arch'], pkid),
                                    callback=handle_results, error_callback=handle_error)
                    pool.close()
                    pool.join()

                # reopen the cache, we need it
                self._cache.reopen()

                hints_dir = os.path.join(self._export_dir, "hints", suite_name, component)
                if not os.path.exists(hints_dir):
                    os.makedirs(hints_dir)
                dep11_dir = os.path.join(self._export_dir, "data", suite_name, component)
                if not os.path.exists(dep11_dir):
                    os.makedirs(dep11_dir)

                # now write data to disk
                hints_fname = os.path.join(hints_dir, "DEP11Hints_%s.yml.gz" % (arch))
                data_fname = os.path.join(dep11_dir, "Components-%s.yml.gz" % (arch))

                hints_f = gzip.open(hints_fname+".new", 'wb')
                data_f = gzip.open(data_fname+".new", 'wb')

                dep11_header = get_dep11_header(suite_name, component, os.path.join(self._dep11_url, component))
                data_f.write(bytes(dep11_header, 'utf-8'))

                for pkg in pkglist:
                    pkid = get_pkg_id(pkg['name'], pkg['version'], pkg['arch'])
                    data = self._cache.get_metadata_for_pkg(pkid)
                    if data:
                        data_f.write(bytes(data, 'utf-8'))
                    hint = self._cache.get_hints(pkid)
                    if hint:
                        hints_f.write(bytes(hint, 'utf-8'))

                data_f.close()
                safe_move_file(data_fname+".new", data_fname)

                hints_f.close()
                safe_move_file(hints_fname+".new", hints_fname)

                all_cpt_pkgs.extend(pkglist)

            # create icon tarball
            self.make_icon_tar(suite_name, component, all_cpt_pkgs)

            log.info("Completed metadata extraction for suite %s/%s" % (suite_name, component))


    def expire_cache(self):
        pkgids = set()
        for suite_name in self._suites_data:
            suite = self._suites_data[suite_name]
            for component in suite['components']:
                for arch in suite['architectures']:
                    pkglist = self._get_packages_for(suite_name, component, arch)
                    for pkg in pkglist:
                        pkid = get_pkg_id(pkg['name'], pkg['version'], pkg['arch'])
                        pkgids.add(pkid)

        # clean cache
        oldpkgs = self._cache.get_packages_not_in_set(pkgids)
        for pkid in oldpkgs:
            pkid = str(pkid, 'utf-8')
            self._cache.remove_package(pkid)
        # ensure we don't leave cruft
        self._cache.remove_orphaned_components()


    def remove_processed(self, suite_name):
        '''
        Delete information about processed packages, to reprocess them later.
        '''

        suite = self._suites_data.get(suite_name)
        if not suite:
            log.error("Suite '%s' not found!" % (suite_name))
            return False

        for component in suite['components']:
            all_cpt_pkgs = list()
            for arch in suite['architectures']:
                pkglist = self._get_packages_for(suite_name, component, arch)

                for pkg in pkglist:
                    package_fname = os.path.join (self._archive_root, pkg['filename'])
                    pkid = get_pkg_id(pkg['name'], pkg['version'], pkg['arch'])

                    # we ignore packages without any interesting metadata here
                    if self._cache.is_ignored(pkid):
                        continue

                    self._cache.remove_package(pkid)

        # drop all components which don't have packages
        self._cache.remove_orphaned_components()


class HTMLGenerator:
    def __init__(self):
        pass


    def initialize(self, dep11_dir):
        dep11_dir = os.path.abspath(dep11_dir)

        conf = load_generator_config(dep11_dir)
        if not conf:
            return False

        self._archive_root = conf.get("ArchiveRoot")

        self._html_url = conf.get("HtmlBaseUrl")
        if not self._html_url:
            self._html_url = "."

        template_dir = os.path.dirname(os.path.realpath(__file__))
        template_dir = os.path.realpath(os.path.join(template_dir, "..", "data", "templates", "default"))
        if not os.path.isdir(template_dir):
            template_dir = os.path.join(sys.prefix, "share", "dep11", "templates")

        self._template_dir = template_dir

        self._distro_name = conf.get("DistroName")
        if not self._distro_name:
            self._distro_name = "Debian"

        self._export_dir = os.path.join(dep11_dir, "export")
        if conf.get("ExportDir"):
            self._export_dir = conf.get("ExportDir")

        if not os.path.exists(self._export_dir):
            os.makedirs(self._export_dir)

        self._suites_data = conf['Suites']

        self._html_export_dir = os.path.join(self._export_dir, "html")

        self._dep11_url = conf.get("MediaBaseUrl")

        os.chdir(dep11_dir)
        return True


    def render_template(self, name, out_dir, out_name = None, *args, **kwargs):
        if not out_name:
            out_path = os.path.join(out_dir, name)
        else:
            out_path = os.path.join(out_dir, out_name)
        # create subdirectories if necessary
        out_dir = os.path.dirname(os.path.realpath(out_path))
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        j2_env = Environment(loader=FileSystemLoader(self._template_dir))

        template = j2_env.get_template(name)
        content = template.render(root_url=self._html_url, distro=self._distro_name,
                                    time=time.strftime("%Y-%m-%d %H:%M:%S %Z"), *args, **kwargs)
        log.debug("Render: %s" % (out_path.replace(self._html_export_dir, "")))
        with open(out_path, 'wb') as f:
            f.write(bytes(content, 'utf-8'))


    def _highlight_yaml(self, yml_data):
        if not yml_data:
            return ""
        if not pygments:
            return yml_data.replace("\n", "<br/>\n")
        return pygments.highlight(yml_data, YamlLexer(), HtmlFormatter())


    def _expand_hint(self, hint_data):
        tag_name = hint_data['tag']
        tag = get_hint_tag_info(tag_name)

        desc = ""
        try:
            desc = tag['text'] % hint_data['params']
        except Exception as e:
            desc = "Error while expanding hint description: %s" % (str(e))

        severity = tag.get('severity')
        if not severity:
            log.error("Tag %s has no severity!", tag_name)
            severity = "info"

        return {'tag_name': tag_name, 'description': desc, 'severity': severity}


    def update_html(self):
        dep11_hintsdir = os.path.join(self._export_dir, "hints")
        if not os.path.exists(dep11_hintsdir):
            return
        dep11_minfodir = os.path.join(self._export_dir, "data")
        if not os.path.exists(dep11_minfodir):
            return

        export_dir = self._html_export_dir
        media_dir = os.path.join(self._export_dir, "media")
        noimage_url = os.path.join(self._html_url, "static", "img", "no-image.png")

        # Render archive suites index page
        self.render_template("suites_index.html", export_dir, "index.html", suites=self._suites_data.keys())

        # TODO: Remove old HTML files
        for suite_name in self._suites_data:
            suite = self._suites_data[suite_name]
            export_dir = os.path.join(self._export_dir, "html", suite_name)

            suite_error_count = 0
            suite_warning_count = 0
            suite_info_count = 0
            suite_metainfo_count = 0

            for component in suite['components']:
                issue_summaries = dict()
                mdata_summaries = dict()
                export_dir_section = os.path.join(self._export_dir, "html", suite_name, component)
                export_dir_issues = os.path.join(export_dir_section, "issues")
                export_dir_metainfo = os.path.join(export_dir_section, "metainfo")

                error_count = 0
                warning_count = 0
                info_count = 0
                metainfo_count = 0

                hint_pages = dict()
                cpt_pages = dict()

                for arch in suite['architectures']:
                    h_fname = os.path.join(dep11_hintsdir, suite_name, component, "DEP11Hints_%s.yml.gz" % (arch))
                    hints_data = None
                    if os.path.isfile(h_fname):
                        f = gzip.open(h_fname, 'r')
                        hints_data = yaml.safe_load_all(f.read())
                        f.close()

                    d_fname = os.path.join(dep11_minfodir, suite_name, component, "Components-%s.yml.gz" % (arch))
                    dep11_data = None
                    if os.path.isfile(d_fname):
                        f = gzip.open(d_fname, 'r')
                        dep11_data = yaml.safe_load_all(f.read())
                        f.close()

                    pkg_index = read_packages_dict_from_file(self._archive_root, suite_name, component, arch)

                    if hints_data:
                        for hdata in hints_data:
                            pkg_name = hdata['Package']
                            pkg_id = hdata.get('PackageID')
                            if not pkg_id:
                                pkg_id = pkg_name
                            pkg = pkg_index.get(pkg_name)
                            maintainer = None
                            if pkg:
                                maintainer = pkg['maintainer']
                            if not maintainer:
                                maintainer = "Unknown"
                            if not issue_summaries.get(maintainer):
                                issue_summaries[maintainer] = dict()

                            hints_raw = hdata.get('Hints', list())

                            # expand all hints to show long descriptions
                            errors = list()
                            warnings = list()
                            infos = list()

                            for hint in hints_raw:
                                ehint = self._expand_hint(hint)
                                severity = ehint['severity']
                                if severity == "info":
                                    infos.append(ehint)
                                elif severity == "warning":
                                    warnings.append(ehint)
                                else:
                                    errors.append(ehint)

                            if not hint_pages.get(pkg_name):
                                hint_pages[pkg_name] = list()

                            # we fold multiple architectures with the same issues into one view
                            pkid_noarch = pkg_id
                            if "/" in pkg_id:
                                pkid_noarch = pkg_id[:pkg_id.rfind("/")]

                            pcid = ""
                            if hdata.get('ID'):
                                pcid = "%s: %s" % (pkid_noarch, hdata.get('ID'))
                            else:
                                pcid = pkid_noarch

                            page_data = {'identifier': pcid, 'errors': errors, 'warnings': warnings, 'infos': infos, 'archs': [arch]}
                            try:
                                l = hint_pages[pkg_name]
                                index = next(i for i, v in enumerate(l) if equal_dicts(v, page_data, ['archs']))
                                hint_pages[pkg_name][index]['archs'].append(arch)
                            except StopIteration:
                                hint_pages[pkg_name].append(page_data)

                                # add info to global issue count
                                error_count += len(errors)
                                warning_count += len(warnings)
                                info_count += len(infos)

                                # add info for global index
                                if not issue_summaries[maintainer].get(pkg_name):
                                    issue_summaries[maintainer][pkg_name] = {'error_count': len(errors), 'warning_count': len(warnings), 'info_count': len(infos)}

                    if dep11_data:
                        for mdata in dep11_data:
                            pkg_name = mdata.get('Package')
                            if not pkg_name:
                                # we probably hit the header
                                continue
                            pkg = pkg_index.get(pkg_name)
                            maintainer = None
                            if pkg:
                                maintainer = pkg['maintainer']
                            if not maintainer:
                                maintainer = "Unknown"
                            if not mdata_summaries.get(maintainer):
                                mdata_summaries[maintainer] = dict()


                            # ugly hack to have the screenshot entries linked
                            #if mdata.get('Screenshots'):
                            #    sshot_baseurl = os.path.join(self._dep11_url, component)
                            #    for i in range(len(mdata['Screenshots'])):
                            #        url = mdata['Screenshots'][i]['source-image']['url']
                            #        url = "<a href=\"%s\">%s</a>" % (os.path.join(sshot_baseurl, url), url)
                            #        mdata['Screenshots'][i]['source-image']['url'] = Markup(url)
                            #        thumbnails = mdata['Screenshots'][i]['thumbnails']
                            #        for j in range(len(thumbnails)):
                            #            url = thumbnails[j]['url']
                            #            url = "<a href=\"%s\">%s</a>" % (os.path.join(sshot_baseurl, url), url)
                            #            thumbnails[j]['url'] = Markup(url)
                            #        mdata['Screenshots'][i]['thumbnails'] = thumbnails


                            mdata_yml = dict_to_dep11_yaml(mdata)
                            mdata_yml = self._highlight_yaml(mdata_yml)
                            cid = mdata.get('ID')

                            # try to find an icon for this component (if it's a GUI app)
                            icon_url = None
                            if mdata['Type'] == 'desktop-app' or mdata['Type'] == "web-app":
                                icon_name = mdata['Icon'].get("cached")
                                cptgid = build_cpt_global_id(cid, mdata.get('X-Source-Checksum'))
                                if icon_name and cptgid:
                                    icon_fname = os.path.join(component, cptgid, "icons", "64x64", icon_name)
                                    if os.path.isfile(os.path.join(media_dir, icon_fname)):
                                        icon_url = os.path.join(self._dep11_url, icon_fname)
                                    else:
                                        icon_url = noimage_url
                                else:
                                    icon_url = noimage_url
                            else:
                                icon_url = os.path.join(self._html_url, "static", "img", "cpt-nogui.png")

                            if not cpt_pages.get(pkg_name):
                                cpt_pages[pkg_name] = list()

                            page_data = {'cid': cid, 'mdata': mdata_yml, 'icon_url': icon_url, 'archs': [arch]}
                            try:
                                l = cpt_pages[pkg_name]
                                index = next(i for i, v in enumerate(l) if equal_dicts(v, page_data, ['archs']))
                                cpt_pages[pkg_name][index]['archs'].append(arch)
                            except StopIteration:
                                cpt_pages[pkg_name].append(page_data)

                                # increase valid metainfo count
                                metainfo_count += 1

                            # check if we had this package, and add to summary
                            pksum = mdata_summaries[maintainer].get(pkg_name)
                            if not pksum:
                                pksum = dict()

                            if pksum.get('cids'):
                                if not cid in pksum['cids']:
                                    pksum['cids'].append(cid)
                            else:
                                pksum['cids'] = [cid]

                            mdata_summaries[maintainer][pkg_name] = pksum

                    if not dep11_data and not hints_data:
                        log.warning("Suite %s/%s/%s does not contain DEP-11 data or issue hints.", suite_name, component, arch)


                # now write the HTML pages with the previously collected & transformed issue data
                for pkg_name, entry_list in hint_pages.items():
                    # render issues page
                    self.render_template("issues_page.html", export_dir_issues, "%s.html" % (pkg_name),
                            package_name=pkg_name, entries=entry_list, suite=suite_name, section=component)

                # render page with all components found in a package
                for pkg_name, cptlist in cpt_pages.items():
                    # render metainfo page
                    self.render_template("metainfo_page.html", export_dir_metainfo, "%s.html" % (pkg_name),
                            package_name=pkg_name, cpts=cptlist, suite=suite_name, section=component)

                # Now render our issue index page
                self.render_template("issues_index.html", export_dir_issues, "index.html",
                            package_summaries=issue_summaries, suite=suite_name, section=component)

                # ... and the metainfo index page
                self.render_template("metainfo_index.html", export_dir_metainfo, "index.html",
                            package_summaries=mdata_summaries, suite=suite_name, section=component)


                validate_result = "Validation was not performed."
                if dep11_data:
                    # do format validation
                    validator = DEP11Validator()
                    ret = validator.validate_file(d_fname)
                    if ret:
                        validate_result = "No errors found."
                    else:
                        validate_result = ""
                        for issue in validator.issue_list:
                            validate_result += issue.replace("FATAL", "<strong>FATAL</strong>")+"<br/>\n"

                # sum up counts for suite statistics
                suite_metainfo_count += metainfo_count
                suite_error_count += error_count
                suite_warning_count += warning_count
                suite_info_count += info_count

                # calculate statistics for this component
                count = metainfo_count + error_count + warning_count + info_count
                valid_perc = 100/count*metainfo_count if count > 0 else 0
                error_perc = 100/count*error_count if count > 0 else 0
                warning_perc = 100/count*warning_count if count > 0 else 0
                info_perc = 100/count*info_count if count > 0 else 0

                # Render our overview page
                self.render_template("section_overview.html", export_dir_section, "index.html",
                            suite=suite_name, section=component, valid_percentage=valid_perc,
                            error_percentage=error_perc, warning_percentage=warning_perc, info_percentage=info_perc,
                            metainfo_count=metainfo_count, error_count=error_count, warning_count=warning_count,
                            info_count=info_count, validate_result=validate_result)


            # calculate statistics for this suite
            count = suite_metainfo_count + suite_error_count + suite_warning_count + suite_info_count
            valid_perc = 100/count*suite_metainfo_count if count > 0 else 0
            error_perc = 100/count*suite_error_count if count > 0 else 0
            warning_perc = 100/count*suite_warning_count if count > 0 else 0
            info_perc = 100/count*suite_info_count if count > 0 else 0

            # Render archive components index/overview page
            self.render_template("sections_index.html", export_dir, "index.html",
                            sections=suite['components'], suite=suite_name, valid_percentage=valid_perc,
                            error_percentage=error_perc, warning_percentage=warning_perc, info_percentage=info_perc,
                            metainfo_count=suite_metainfo_count, error_count=suite_error_count, warning_count=suite_warning_count,
                            info_count=suite_info_count)

        # Copy the static files
        target_static_dir = os.path.join(self._export_dir, "html", "static")
        shutil.rmtree(target_static_dir, ignore_errors=True)
        shutil.copytree(os.path.join(self._template_dir, "static"), target_static_dir)


def main():
    """Main entry point of generator"""

    apt_pkg.init()

    parser = ArgumentParser(description="Generate DEP-11 metadata from Debian packages.")
    parser.add_argument('subcommand', help="The command that should be executed.")
    parser.add_argument('parameters', nargs='*', help="Parameters for the subcommand.")

    parser.usage = "\n"
    parser.usage += " process [CONFDIR] [SUITE] - Process packages and extract metadata.\n"
    parser.usage += " cleanup [CONFDIR]         - Remove unused data from the cache and expire media.\n"
    parser.usage += " update-html [CONFDIR]     - Re-generate the metadata and issue HTML pages.\n"
    parser.usage += " removed-processed [CONFDIR] [SUITE] - Remove information about processed or failed components.\n"

    args = parser.parse_args()
    command = args.subcommand
    params = args.parameters

    # configure logging
    log_level = log.INFO
    if os.environ.get("DEBUG"):
        log_level = log.DEBUG
    log.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=log_level)

    if command == "process":
        if len(params) != 2:
            print("Invalid number of arguments: You need to specify a DEP-11 data dir and suite.")
            sys.exit(1)
        gen = DEP11Generator()
        ret = gen.initialize(params[0])
        if not ret:
            print("Initialization failed, can not continue.")
            sys.exit(2)

        gen.process_suite(params[1])

    elif command == "cleanup":
        if len(params) != 1:
            print("Invalid number of arguments: You need to specify a DEP-11 data dir.")
            sys.exit(1)
        gen = DEP11Generator()
        ret = gen.initialize(params[0])
        if not ret:
            print("Initialization failed, can not continue.")
            sys.exit(2)

        gen.expire_cache()

    elif command == "update-html":
        if len(params) != 1:
            print("Invalid number of arguments: You need to specify a DEP-11 data dir.")
            sys.exit(1)
        hgen = HTMLGenerator()
        ret = hgen.initialize(params[0])
        if not ret:
            print("Initialization failed, can not continue.")
            sys.exit(2)

        hgen.update_html()

    elif command == "remove-processed":
        if len(params) != 2:
            print("Invalid number of arguments: You need to specify a DEP-11 data dir and suite.")
            sys.exit(1)
        gen = DEP11Generator()
        ret = gen.initialize(params[0])
        if not ret:
            print("Initialization failed, can not continue.")
            sys.exit(2)

        gen.remove_processed(params[1])
    else:
        print("Run with --help for a list of available command-line options!")
