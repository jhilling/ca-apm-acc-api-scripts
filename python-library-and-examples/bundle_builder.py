#!/usr/bin/env python

#TODO extract feature (for windows benefit?)
#TODO meta display feature (for windows benefit?)

"""
Bundle file manipulator.

Create and manipulate APM ACC Bundle files.

File to bundle file mapping rules (see get_archive_target_dest())


1) Does the input path contain "wily"?

    File path mapped into bundle directly.

2) Is the file a well known name?

    File automatically put into appropriate location based on a mapping

3) Is the file a known type (pbd, pbl, jar)?

    File automatically put into appropriate location based on a mapping

else:
   throw an error

Can optionally specify tools, ext parameters etc to map files into the archive cases
for cases where mapping not automatically determined, or if files are required to be
placed in unusual locations

"""


from __future__ import print_function

import argparse
import os
import StringIO
import time
import tempfile
import shutil
import json
import tarfile
import getpass
import socket

from distutils.version import LooseVersion


bundle_file_map = {
    "description.md": "metadata/description.md",
    "Agent.jar":      "wily/Agent.jar",
    "APM_ThirdPartyLicensingDoc.txt": "APM_ThirdPartyLicensingDoc.txt"
}

bundle_ext_map = {
    ".md":      "metadata",
    ".bundle":  "metadata",
    ".jar":     "wily/core/ext",
    ".pbd":     "wily/core/config",
    ".pbl":     "wily/core/config",
    ".profile": "wily/core/config"
}


class BundleFileMapper(object):

    def split_path(self, the_path):
        """Split a path into a list"""

        path_split = []

        while True:
            the_path, file = os.path.split(the_path)
            # print("path_abs", path_abs)
            # print("file", file)

            path_split.insert(0, file)

            if not the_path or the_path == "/":
                break

        # print("path_split", path_split)
        return path_split

    def file_to_bundle(self, path):
        """
        Try and identify where the file goes from the filename
        """
        head, filename = os.path.split(path)

        return bundle_file_map[filename]

    def ext_to_bundle(self, path):
        """
        Try and identify where the file goes from the extension
        """
        root, ext = os.path.splitext(path)
        return os.path.join(bundle_ext_map[ext], os.path.split(path)[1])

    def wily_to_bundle(self, path):
        path_split = self.split_path(path)
        return os.path.join(*path_split[path_split.index("wily"):])

    def get_archive_target_dest(self, path):

        """
        Try and determine where in the bundle the given arbitrary path should go
        by trying several different approaches
        """

        archive_target_dest = None

        # print("mapping", path)

        try:
            # Found wily in the path, so return the path from wily onwards
            archive_target_dest = self.wily_to_bundle(path)
        except ValueError:
            try:
                archive_target_dest = self.file_to_bundle(path)
            except KeyError:
                try:
                    archive_target_dest = self.ext_to_bundle(path)
                except KeyError:
                    raise Exception("Could not map path in bundle for: " + path)

        # print("target dest for %s is %s" % (path, archive_target_dest))
        return archive_target_dest


class BundleVersion(object):

    def __init__(self, version_string):
        lv = LooseVersion(version_string)

        if len(lv.version) > 4:
            raise Exception("Unsupported version format")

        self.bundle_version = [0, 0, 0, 0]

        try:
            self.bundle_version[0] = self.munge(lv.version[0])
            self.bundle_version[1] = self.munge(lv.version[1])
            self.bundle_version[2] = self.munge(lv.version[2])
            self.bundle_version[3] = self.munge(lv.version[3])
        except IndexError:
            pass

    def munge(self, ver_comp):
        """Make sure all version components are integers, otherwise set to 0"""
        i = 0;
        try:
            i = int(ver_comp)
        except ValueError:
            pass
        return i

    def increment(self):
        self.bundle_version[3] += 1
        return self.__str__()

    def __str__(self):
        return ".".join( [ str(v) for v in self.bundle_version ] )


class BundleSpec(object):

    def __init__(self, name=None):

        if not name:
            name = "bundle"

        j = self.j = {}

        j["name"] = name
        j["author"] = "%s@%s" % (getpass.getuser(), socket.getfqdn())
        j["displayName"] = name
        j["version"] = "10.4.0.0"
        j["facets"] = ["generated"]
        j["dependencies"] = ["java-agent", "process"]
        j["enhances"] = ["java-agent"]
        j["specificationVersion"] = "2"
        j["compatibility"] = {"agentVersion": ">=10.2", "osName": ["windows", "unix"]}
        j["type"] = "java"
        j["dynamic"] = False

    def load(self, fileobj):
        self.j.update(json.load(fileobj))

    def increment_version(self):
        self.j["version"] = BundleVersion(self.j["version"]).increment()
        return self.j["version"]

    def meta_json(self):
        return json.dumps(self.j, indent=2, sort_keys=2)

    def __getitem__(self, key):
        """
        This enables us to use the index operator, eg: spec["name"]
        """
        try:
            value = self.j[key]
            return value
        except KeyError:
            print("ERROR: Missing key %s.  Attributes available in the json are:\n%s" % (key, self.j))
            raise


class BundleBuilder(object):
    def __init__(self, name, force_overwrite_existing):
        self.update_bundle_version = True
        self.force_overwrite_existing = force_overwrite_existing

        self.spec = BundleSpec(name)

        self.tar_name = None
        self.tar_name_temp = tempfile.mktemp()

        self.tar = tarfile.open(self.tar_name_temp, "w:gz")
        self.added_spec = self.added_desc = self.added_toggles = False
        self.added = {}

    def description_template(self):
        return """This is the template description file for %(name)s
        """ % self.spec.j

    def _add(self, origin, tarinfo, fileobj=None):

        archive_filename = tarinfo.name

        if self.added.get(archive_filename):
            raise Exception("Already added: " + archive_filename)

        if fileobj:
            local_entity_name = fileobj.name
        else:
            local_entity_name = tarinfo.name

        if local_entity_name == archive_filename:
            print("[%s] %s" % (origin, archive_filename))
        else:
            print("[%s] %s -> %s" % (origin, local_entity_name, archive_filename))

        if fileobj:

            if os.path.basename(archive_filename).find("toggles") >= 0:

                if self.added_toggles:
                    # TODO this is a hack. A bundle cannot have multiple toggles files, so rename them.
                    tarinfo.name = archive_filename = "%s/%s" % (os.path.dirname(archive_filename), os.path.basename(archive_filename).replace("toggles", "TOGGLES"))
                    print("WARNING: Have already added a toggles file. Hiding this one in:", archive_filename)
                else:
                    self.added_toggles = True

            elif archive_filename.startswith("metadata/"):
                if archive_filename.endswith(".bundle"):

                    if self.added_spec:
                        raise Exception("Multiple bundle spec files loaded")

                    self.added_spec = True

                    # update our spec with this one so we have the correct name
                    self.spec.load(fileobj)

                    v1 = self.spec["version"]
                    print("Bundle version is %s" % v1)

                    fileobj.seek(0)

                    if self.update_bundle_version:
                        # here we tweak the metadata file to increment the version of the bundle

                        v2 = self.spec.increment_version()
                        print("Updating bundle version to %s" % v2)

                        # Now we will substitute given fileobj with our updated version of the bundle meta file,
                        # and this is the one that will be written into the bundle
                        # content = pformat(j, indent=2) + "\n"
                        # content = json.dumps(j, sort_keys=True, indent=4, separators=(',', ': '))
                        content = self.spec.meta_json()

                        # print(content)
                        tarinfo.size = len(content)
                        fileobj = StringIO.StringIO(content)
                        fileobj.name = archive_filename

                elif os.path.basename(archive_filename) == "description.md":
                    self.added_desc = True

        self.tar.addfile(tarinfo, fileobj)

        self.added[archive_filename] = True

    def add_file(self, local_filename, archive_filename=None):
        """
        archive_filename is the final name in the tar
        local_filename is the filename
        """

        if self.tar_name and os.path.exists(self.tar_name) and os.path.samefile(self.tar_name, local_filename):
            # raise Exception("Adding bundle file to bundle")
            print("Skipping", local_filename)
            return

        if not archive_filename:
            archive_filename = BundleFileMapper().get_archive_target_dest(os.path.abspath(local_filename))

        tarinfo = self.tar.gettarinfo(local_filename, archive_filename)
        tarinfo.uname = tarinfo.gname = ""
        tarinfo.uid = tarinfo.gid = 0

        with open(local_filename, "rb") as fileobj:
            self._add("file", tarinfo, fileobj)

        return tarinfo

    def add_string(self, content, archive_filename):

        # print("[string] -> %s" % archive_filename)

        tarinfo = tarfile.TarInfo(archive_filename)
        tarinfo.size = len(content)
        tarinfo.mtime = time.time()

        fileobj = StringIO.StringIO(content)
        fileobj.name = archive_filename

        self._add("string", tarinfo, fileobj)

    def add_tarinfo_entry(self, tar, tarinfo):
        archive_filename = BundleFileMapper().get_archive_target_dest(tarinfo.name)

        fileobj = None
        if not tarinfo.isdir():
            fileobj = tar.extractfile(tarinfo)

        # We do not particularly want to inherit the user/group settings in the bundle
        tarinfo.uname = tarinfo.gname = ""
        tarinfo.uid = tarinfo.gid = 0
        tarinfo.name = archive_filename

        self._add("tar", tarinfo, fileobj)

    def close(self):

        # Add the bundle spec etc

        if self.tar:
            if self.added_spec:
                print("Bundle spec file already added, not generating one")
            else:
                print("No bundle spec file added, adding template bundle metadata file")
                self.add_string(self.spec.meta_json(), "metadata/%s.bundle" % self.spec["name"])

            if self.added_desc:
                print("Description file already added, not generating one")
            else:
                print("No description.md file added, adding template description.md file")
                self.add_string(self.description_template(), "metadata/description.md")

            self.tar.close()

            if not self.tar_name:
                self.tar_name = "%s-%s.tar.gz" % (self.spec["name"], self.spec["version"])

            if not self.force_overwrite_existing and os.path.exists(self.tar_name):
                raise Exception("Output file already exists (use -f to overwrite): " + self.tar_name)

            shutil.move(self.tar_name_temp, self.tar_name)
            print("%s -> %s" % (self.tar_name_temp, self.tar_name))


class App(object):
    """
    Bundle file manipulator. Create bundles from the given files.
    Tar files will automatically be unarchived and the contents
    added to the bundle.

    Bundles are small pieces of Agent which are
    combined together to make a complete APM Agent Package which can then
    be downloaded and deployed (see packages.py)
    """

    def __init__(self):
        self.args = self.build_arg_parser()

    def description(self):
        """
        Use the class doc string for the --help info
        """
        return self.__doc__

    def build_arg_parser(self):
        """
        Add some more args to the standard set
        """
        # super(App, self).build_arg_parser()
        self.parser = argparse.ArgumentParser(description=self.description())

        self.parser.add_argument('-v', '--verbose', action='store_true', help="be more verbose")

        self.parser.add_argument('-f', '--force', action='store_true', help="override existing bundle file")

        self.parser.add_argument('--no-version-update', action='store_true', help="do not update version number of bundle")

        self.parser.add_argument('-n', '--name', action='store', help="Bundle name")

        # To force files to particular directories in the bundle, you can specify them uses these switches
        # Add more directories here if needed.
        self.parser.add_argument('-t', '--tools', action='append', default=[], help="put file under wily/core/tools")
        self.parser.add_argument('-e', '--ext', action='append', default=[], help="put file under wily/core/ext")
        self.parser.add_argument('-c', '--config', action='append', default=[], help="put file under wily/core/config")
        # etc

        self.parser.add_argument('bundle_files', metavar='FILE', nargs='*', type=str,
                                 help='files to add to the bundle')

        return self.parser.parse_args()

    def check_files_exist(self, *args):
        for arg in args:
            for file in arg:
                if not os.path.exists(file):
                    raise Exception("File does not exist: " + file)

    def main(self):

        bundle = BundleBuilder(self.args.name, self.args.force)

        bundle.update_bundle_version = not self.args.no_version_update

        self.check_files_exist(self.args.bundle_files, self.args.tools, self.args.ext, self.args.config)

        for bundle_file in self.args.bundle_files:

            if bundle_file.endswith(".tar") or bundle_file.endswith("tar.gz"):
                with tarfile.open(bundle_file) as btf:
                    for ti in btf.getmembers():
                        bundle.add_tarinfo_entry(btf, ti)
            else:
                if os.path.isdir(bundle_file):
                    # walk the specified directory
                    for root, dir_names, files in os.walk(bundle_file):
                        if not files and not dir_names:
                            # Add empty directory
                            bundle.add_file(os.path.join(root))
                        else:
                            for file in files:
                                bundle.add_file(os.path.join(root, file))
                else:
                    bundle.add_file(bundle_file)

        for tool in self.args.tools:
            bundle.add_file(tool, os.path.join("wily/core/tools", os.path.basename(tool)))

        for ext in self.args.ext:
            bundle.add_file(ext, os.path.join("wily/core/ext", os.path.basename(ext)))

        for config in self.args.config:
            bundle.add_file(config, os.path.join("wily/core/config", os.path.basename(config)))

        # (Add more wily subdirectories as required)

        bundle.close()


if __name__ == "__main__":
    App().main()
