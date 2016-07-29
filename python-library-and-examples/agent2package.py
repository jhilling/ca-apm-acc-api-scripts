#!/usr/bin/env python


# Convert an agent installation to an equivalent package, including creating overrides.

# TODO needs some smarts around toggles/files? toggles files need to handled like profiles/properties with overrides being created as necessary.

# TODO if there is more than one profile, need to nominate one, or error/warn
# TODO rather than ask user about import of toggle, try and work it out from the profile.

from __future__ import print_function

import os
import sys
import tarfile
import re
import hashlib
import tempfile

from distutils.version import LooseVersion

import pyacc
import bundle_builder


agent_file_map = {
    "wily/installInstructions.md":     None,
    "wily/core/config/acc-master.pbl": None,
    "wily/manifest.txt":               None
}

agent_property_map = {
    "acc.package.id":      None,
    "acc.package.name":    None,
    "acc.package.version": None,
    "introscope.autoprobe.directivesFile": None
}


class BundleMappingException(Exception):
    pass


class AmbiguousBundleMappingException(BundleMappingException):
    pass


class BundleAlreadyIncludedException(BundleMappingException):
    def __init__(self, bundle):
        self.bundle = bundle


class NoAvailableBundleMappingException(BundleMappingException):
    pass


class BundleIndex(object):

    def __init__(self, acc, args):
        self.acc = acc
        self.args = args

        self.bundle_files = self.fetch_bundles()

    def get_filename_map(self):
        return self.index_bundles(self.bundle_files)

    def fetch_bundles(self):
        print("\nFetching bundles:")
        bundle_files = []
        for bundle in self.acc.bundles(size=200):
            print("\t%s:%s" % (bundle["name"], bundle["version"]))
            if self.args.verbose:
                bundle.get_json()
                print(bundle)

            temp_dir = "%s/bundle_temp_%s" % (tempfile.gettempdir(), hashlib.sha1(self.acc.server).hexdigest())
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            bundle.filename = bundle.download(directory=temp_dir)
            bundle_files.append(bundle)
        return bundle_files

    def index_bundles(self, bundle_files):
        print("\nIndexing bundles:")
        filename_map = {}
        for bundle in bundle_files:
            print("\n\t%s:%s (%s)" % (bundle["name"], bundle["version"], bundle.filename))
            btf = tarfile.open(bundle.filename)
            for ti in btf.getmembers():
                if ti.isdir() or ti.name.startswith("metadata/"):
                    pass
                else:
                    print("\t\t%s" % ti.name)
                    entries = filename_map.setdefault(ti.name, [])
                    entries.append(bundle)
            btf.close()
        return filename_map


class PackageUtil(object):

    def __init__(self, new_package):
        self.new_package = new_package

    def get_compatible_bundles(self):

        """
        Build a map of the highest version of the package compatible bundles
        """

        print("\nThese are the compatible bundles for the empty package:")
        compatible_bundles = {}
        for bundle in self.new_package.compatible_bundles():
            print("\t%s:%s" % (bundle["name"], bundle["version"]))

            exist = compatible_bundles.get(bundle["name"])

            if not exist:
                compatible_bundles[bundle["name"]] = bundle
            else:
                # Select the higher version
                if LooseVersion(bundle["version"]) > LooseVersion(exist["version"]):
                    print("\t\tSelecting %s over %s" % (bundle["version"], exist["version"]))
                    compatible_bundles[bundle["name"]] = bundle

        return compatible_bundles

    def get_required_bundles(self):

        print("\nThese are the required bundles:")
        required_bundles = {}
        for bundle in self.new_package.required_bundles():
            print("\t%s:%s" % (bundle["name"], bundle["version"]))
            required_bundles[bundle["name"]] = bundle

        return required_bundles


class AgentProfile(object):
    def __init__(self, fileobj):
        self.fileobj = fileobj

    def properties(self):
        for raw in self.fileobj:
            line = raw.strip()
            if not line or line[0] == "#":
                pass
            else:
                try:
                    prop_split = App.val_re.split(line)
                    hidden = prop_split[1] == "#"
                    name = prop_split[2].strip()
                    value = prop_split[3].strip()

                    yield hidden, name, value
                except IndexError as e:
                    print("Failed to process", line.strip())


class AgentToggles(object):
    def __init__(self, fileobj):
        self.fileobj = fileobj

    def properties(self):
        # TurnOn: ServerInfoTracing
        for raw in self.fileobj:
            line = raw.strip()
            if not line or line[0] == "#":
                pass
            elif line.startswith("InstrumentPoint:"):
                pass
            elif line.startswith("TurnOn: "):
                try:
                    name = "instrument.%s" % line.split(" ", 1)[1]
                    yield False, name, "on"
                except IndexError as e:
                    print("Failed to process", line.strip())
            else:
                raise Exception("Not sure what this is in toggles file: " + line)

class AgentArchiverDecomposer(object):

    def __init__(self, acc, args, agent_archive, filename_map):
        self.acc = acc
        self.args = args

        self.agent_archive = agent_archive
        self.filename_map = filename_map

        self.warnings = 0

        self.new_package = None

        self.bundle_missing = None

        # A map of of properties in the archive to bundles that have the property
        self.archive_property_to_bundles = {}
        self.archive_hidden_property_to_bundles = {}

        # A map of properties found in the archive.
        self.archive_property_values = {}
        self.archive_hidden_property_values = {}

        # ?
        self.property_to_bundle_map = None

        # A map of ?
        self.included_filename_map = {}

        # A map of the bundles that are compatible with the new Package we have created.
        # Only the latest version of a given bundle is included here.
        self.compatible_bundles = None

        # A list of bundles we are going to add to the Package, initialised to the required bundles.
        self.included_bundles = None

        # A map of properties to (compatible) bundles
        self.bundle_property_map = {}
        # self.hidden_bundle_property_map = {}

    def _build_bundle_property_map(self, compatible_bundles):

        """
        Build a map of properties for the compatible bundles
        Does not consider multiple versions of the bundle
        as compatible_bundles has already been whittled down to a single version.
        """

        print("\nReading properties for compatible bundles:")
        for bundle in compatible_bundles:
            bundle.profile_property_map = {}

            print("\t%s:%s" % (bundle["name"], bundle["version"]))
            profile = bundle.profile()

            for prop in profile["properties"] or []:

                print("\t\t%s%s=%s" % ("#" if prop["hidden"] else "", prop["name"], prop["value"]))

                if prop["hidden"]:
                    if prop["name"] not in bundle.profile_property_map:
                        bundle_entry = self.bundle_property_map.setdefault(prop["name"], {})
                        bundle_entry[bundle["name"]] = bundle

                        bundle.profile_property_map[prop["name"]] = prop
                    else:
                        print("ignoring", prop)
                else:
                    bundle_entry = self.bundle_property_map.setdefault(prop["name"], {})
                    bundle_entry[bundle["name"]] = bundle

                    bundle.profile_property_map[prop["name"]] = prop

    def create_package_from_archive(self):

        print("\nCreating empty package:")

        v2 = ".".join(self.args.agent_version.split(".")[0:2])

        # Create draft package. This enables us to get the required/compatible/includes list of bundles
        self.new_package = self.acc.package_create(name=os.path.basename(self.agent_archive),
                                                   os=self.args.os,
                                                   appserver=self.args.appserver,
                                                   em_host=self.args.em_host,
                                                   process_display_name="process display name",
                                                   agent_version=v2,
                                                   comment="Package generated from Agent archive " +
                                                           os.path.basename(self.agent_archive), draft=True)
        print(self.new_package)

        self.compatible_bundles = PackageUtil(self.new_package).get_compatible_bundles()
        self.included_bundles = PackageUtil(self.new_package).get_required_bundles()

        self._build_bundle_property_map(self.compatible_bundles.values())

        self.process_agent_archive()

        # Pick bundles for the files we have
        self.resolve_bundles(["file", "files"], self.included_filename_map)

        # Pick bundles for the properties we have
        self.property_to_bundle_map = self.resolve_bundles(["property", "properties"],
                                                           self.archive_property_to_bundles)

        # Finally try and map properties to hidden property values in the bundle properties
        self.hidden_property_to_bundle_map = self.resolve_bundles(["hidden property", "hidden properties"],
                                                                  self.archive_hidden_property_to_bundles)

        # Resolve remaining dependencies
        self.resolve_dependencies()

        override_count = self.create_overrides()
        override_count += self.create_overrides_to_hide_extra_properties()

        # Now add the bundles we selected to the package
        self.add_bundles_to_package(self.included_bundles.values())

        # Add the overrides TODO really want to do this as part of the same patch, rather than 2 patches
        if override_count > 0:
            print("\nAdding %d overrides" % override_count)
            self.new_package.add_overrides(self.new_package["bundleOverrides"])

    def _agent_archive_entries(self):
        """Loop over each file in the archive"""
        with tarfile.open(self.agent_archive) as atf:
            for ti in atf:
                if ti.isdir():
                    pass
                else:
                    yield atf, ti

    def process_agent_archive(self):
        """
        Read the agent archive
        """

        print("\nAnalyzing Agent Package: %s" % self.agent_archive)
        for atf, ti in self._agent_archive_entries():

            if ti.name.endswith(".profile"):
                print("\tFound profile:", ti.name)
                for hidden, name, value in AgentProfile(atf.extractfile(ti)).properties():
                    self.register_archive_property(hidden, name, value)

            elif "toggle" in ti.name:

                # Importing toggles-full.pbd could be a bad idea as it will switch everything on

                print("\tDo you wish to include the content of toggles file: %s" % ti.name)

                ans = "y" if ti.name == "wily/core/config/acc-master-toggles.pbd" or self.args.yes else raw_input("y/N: ")

                if ans == "y":
                    for hidden, name, value in AgentToggles(atf.extractfile(ti)).properties():
                        self.register_archive_property(hidden, name, value)
                else:
                    print("skipping import of toggles from %s" % ti.name)

            else:
                # Check if a mapping exists. Some files are mapped to None which means ignore them
                # and don't try and add them to the package
                if ti.name not in agent_file_map:
                    entries = self.filename_map.get(ti.name)
                else:
                    map_dest = agent_file_map[ti.name]
                    if not map_dest:
                        print("\tIgnoring file: %s" % ti.name)
                        continue
                    entries = self.filename_map.get(map_dest)

                if not entries:
                    print("\tWARNING: No bundle mapping for: %s" % (ti.name))
                    self.warnings += 1

                    if not self.bundle_missing:
                        name = "%s-unknown-files" % os.path.splitext(os.path.basename(self.agent_archive))[0]
                        self.bundle_missing = bundle_builder.BundleBuilder(name, force_overwrite_existing=True)

                    self.bundle_missing.add_tarinfo_entry(atf, ti)
                else:
                    print("\t%s : Provided by: %s" % (ti.name, ["%s:%s" % (x["name"], x["version"]) for x in entries or []]))
                    self.included_filename_map[ti.name] = entries

        if self.bundle_missing:
            self.bundle_missing.close()

    def register_archive_property(self, hidden, name, value):

        if name in agent_property_map:
            name_map = agent_property_map.get(name)
            if not name_map:
                print("\t\tIgnoring property: %s" % name)
                return
            name = name_map

        if hidden:
            self.register_hidden_archive_property(name, value)
        else:
            # Save the original property
            self.archive_property_values[name] = value

            print("\t\tSearching for property: %s(=%s)" % (name, value))

            bundle_map = self.bundle_property_map.get(name)

            if not bundle_map:
                print("\t\t\tCould not find the property. This will be added as an override.")
            else:
                print("\t\t\tFound property %s in %d bundles (%s)" % (name, len(bundle_map),
                    ["%s:%s" % (bundle["name"], bundle["version"]) for bundle in bundle_map.itervalues()]))

                self.archive_property_to_bundles[name] = bundle_map.values()

    def register_hidden_archive_property(self, name, value):
        # Save the original property
        self.archive_hidden_property_values[name] = value

        print("\t\tSearching for hidden property: %s(=%s)" % (name, value))

        bundle_map = self.bundle_property_map.get(name)

        if not bundle_map:
            print("\t\t\tCould not find the property. This will be added as an override.")
        else:
            print("\t\t\tFound property %s in %d bundles (%s)" % (name, len(bundle_map),
                ["%s:%s" % (bundle["name"], bundle["version"]) for bundle in bundle_map.itervalues()]))

            self.archive_hidden_property_to_bundles[name] = bundle_map.values()

    def resolve_bundles(self, mapping_type, included_filename_map):

        """Resolve files or properties to the bundle they belong to"""

        bundle_map = {}

        # Add bundles from files
        for i in range(len(included_filename_map)):

            print("\nMapping %s to bundles, iteration %d" % (mapping_type[1], i))

            # Keep looping trying to uniquely resolve the files of the agent archive to
            # bundles.  If there is ambiguity for a certain file, we defer resolution and
            # continue and try and resolve it afterwards whereby a bundle may have since been
            # added which provides the uncertain file.
            remaining = added = no_mapping = mapped = 0

            for filename_or_property, entries in included_filename_map.iteritems():

                try:
                    candidate = self.choose_bundle(entries)

                    # Add the matched bundle to the included bundles list
                    print("\tAdding Bundle %s:%s to Package due to %s %s" % (candidate["name"], candidate["version"], mapping_type[0], filename_or_property))
                    self.included_bundles[candidate["name"]] = candidate

                    bundle_map[filename_or_property] = candidate

                    added += 1

                except BundleAlreadyIncludedException as e:
                    bundle_map[filename_or_property] = e.bundle
                    mapped += 1
                except NoAvailableBundleMappingException:
                    no_mapping += 1
                except AmbiguousBundleMappingException:
                    print("\tCould not resolve %s %s to a unique bundle (yet)" % (mapping_type[0], filename_or_property))
                    remaining += 1

            if remaining == 0:
                # Nothing left to resolve
                print("%d %s successfully mapped to bundles. %d remain unmapped" % (mapped, mapping_type[1], no_mapping))
                break

            if added == 0:
                raise AmbiguousBundleMappingException("Could not satisfactorily resolve some %s to a bundle" % (mapping_type[1]))

            print("Have %d %s that could not resolve to a bundle on iteration %d" % (remaining, mapping_type[1], i))

        return bundle_map

    def resolve_dependencies(self):
        print("\nResolve remaining dependencies for Package\n")

        facets_included_map = {}
        for bundle in self.included_bundles.values():
            for facet in bundle["facets"]:
                facets_included_map.setdefault(facet, []).append(bundle)

        facets_compatible_map = {}
        for bundle in self.compatible_bundles.values():
            for facet in bundle["facets"]:
                facets_compatible_map.setdefault(facet, []).append(bundle)

        for bundle in self.included_bundles.values():
            self.resolve_dependency_recursive(facets_included_map, facets_compatible_map, bundle)

    def resolve_dependency_recursive(self, facets_included_map, facets_compatible_map, bundle):

        print("Bundle %s" % bundle["name"])

        for dep in bundle["dependencies"]:

            dep_implemented_by = facets_included_map.get(dep)

            if not dep_implemented_by:

                # Search compatible bundles for that one
                entries = facets_compatible_map.get(dep)

                candidate = self.choose_bundle(entries)

                print("\tNeed to additionally include %s:%s" % (candidate["name"], candidate["version"]))
                self.included_bundles[candidate["name"]] = candidate

                for facet in candidate["facets"]:
                    facets_included_map.setdefault(facet, []).append(candidate)

                # Now recurse for the one we added
                self.resolve_dependency_recursive(facets_included_map, facets_compatible_map, candidate)
            else:
                dep_implemented_by = [b["name"] for b in dep_implemented_by]
                print("\tdepends on: %s which is provided by: %s" % (dep, dep_implemented_by))

    def choose_bundle(self, entries):

        candidate = None

        for bundle in entries:
            exist = self.included_bundles.get(bundle["name"])
            if exist:
                # if something is already providing this file, stop searching
                raise BundleAlreadyIncludedException(bundle)
            else:
                compat = self.compatible_bundles.get(bundle["name"])

                if compat and bundle["version"] == compat["version"]:

                    if candidate:
                        raise AmbiguousBundleMappingException("Have multiple compatible bundles which could provide that file or property")
                    else:
                        candidate = bundle

        if not candidate:
            raise NoAvailableBundleMappingException()

        return candidate

    def create_overrides(self):

        master = self.new_package["bundleOverrides"]

        print("\nCreate overrides for properties found in the agent archive\n")

        override_count = 0

        # Need to check what the value in the profile and the bundle are.
        # If they are != then we need to create an override
        for property_name, value in self.archive_property_values.iteritems():

            # print("\tFrom archive: %s=%s" % (property, value))

            # What is the value in the bundle?
            bundle = self.property_to_bundle_map.get(property_name)

            if not bundle:

                if property_name.startswith("instrument."):
                    print("\tWARNING: toggle %s=%s cannot be found in any bundles" % (property_name, value))
                    self.warnings += 1
                    continue

                print("\tProperty %s is not fulfilled by any bundle, need to create an override to create the property" % property_name)

                overrides = master.setdefault("java-agent", {"preamble": None, "properties":[]})

                prop_dic = {"description": None,
                            "hidden": False,
                            "name":  property_name,
                            "value": value,
                            "userKey": "+"}

                overrides["properties"].append(prop_dic)

                override_count += 1
            else:
                bundle_property = bundle.profile_property_map[property_name]
                bundle_value = bundle_property["value"] or ""

                print("\tProperty %s=%s is fulfilled by bundle %s:%s as %s%s=%s" % (property_name, value, bundle["name"], bundle["version"], "#" if bundle_property["hidden"] else "", bundle_property["name"], bundle_value))

                if value != bundle_value or bundle_property["hidden"]:
                    print("\t\tValues differ - will create an override: %s=%s\n" % (property_name, value))

                    overrides = master.setdefault(bundle["name"], {"preamble": None, "properties":[]})

                    prop_dic = {"description": None,
                                "hidden": False,
                                "name":  property_name,
                                "value": value,
                                "userKey": bundle_property["key"]}

                    overrides["properties"].append(prop_dic)

                    override_count += 1

        return override_count

    def create_overrides_to_hide_extra_properties(self):
        print("\nChecking for extra properties in included bundles which need to be hidden in the Package\n")

        master = self.new_package["bundleOverrides"]
        override_count = 0

        for bundle in self.included_bundles.values():

            print("Bundle %s:%s" % (bundle["name"], bundle["version"]))

            # TODO OPT we're fetching the profile again. the bundle in the compatible_bundles list should alraedy have it in there.
            profile = bundle.profile()

            for prop in profile["properties"] or []:
                if not prop["hidden"]:
                    if prop["name"] in agent_property_map and not agent_property_map.get(prop["name"]):
                        print("\tIgnoring property: %s" % prop["name"])
                        continue

                    # Check if this property existing in the original agent package.
                    # If it DIDN'T exist, then hide it.
                    if not prop["name"] in self.archive_property_values:
                        print("\thiding %s=%s" % (prop["name"], prop["value"]))

                        overrides = master.setdefault(bundle["name"], {"preamble": None, "properties":[]})

                        prop_dic = {"description": None,
                                    "hidden": True,
                                    "name":  prop["name"],
                                    "value": prop["value"] or "",
                                    "userKey": None}

                        overrides["properties"].append(prop_dic)

                        override_count += 1

        return override_count

    def add_bundles_to_package(self, bundles):

        print("\nAdding %d bundles to package:" % len(bundles))
        for bundle in bundles:
            print("\t%s:%s" % (bundle["name"], bundle["version"]))

        self.new_package.add_bundles([b["id"] for b in bundles])


class App(pyacc.AccCommandLineApp):

    appservers = ["other", "ctg-server", "glassfish", "interstage", "jboss", "tomcat", "weblogic", "websphere"]
    val_re = re.compile("^([#]?)(.*)=(.*)")

    """
    Convert an agent installation to an equivalent package, including creating overrides and optionally download it.
    """
    def build_arg_parser(self):
        """
        Add some more args to the standard set
        """
        super(App, self).build_arg_parser()

        self.parser.add_argument('-v', '--verbose', action='store_true', help="be more verbose")

        self.parser.add_argument('--appserver', action='store', default="tomcat", help="appserver type", choices=App.appservers)
        self.parser.add_argument('--os', action='store', help="os type", default="unix", choices=["unix", "windows"])
        self.parser.add_argument('--em-host', action='store', help="Alternate EM host name for new package", default=None)
        self.parser.add_argument('--agent-version', action='store', help="agent version", default="10.2")
        self.parser.add_argument('--format', action='store',
                                 help='write files in the given format. "archive" means zip for windows packages, tar.gz for unix packages',
                                 default="archive", choices=["zip", "tar", "tar.gz", "archive"])

        self.parser.add_argument('-d', '--download', action='store_true', help="Download package after creating it")

        self.parser.add_argument('-y', '--yes', action='store_true', help="Answer Yes to any questions")

        self.parser.add_argument('agent', metavar='AGENT', nargs='*', type=str, help='Agent Package')

    def main(self):

        for agent_archive in self.args.agent:
            if not os.path.exists(agent_archive):
                print("ERROR: %s does not exist" % agent_archive)
                sys.exit(1)

        filename_map = BundleIndex(self.acc, self.args).get_filename_map()

        for agent_archive in self.args.agent:

            aad = AgentArchiverDecomposer(self.acc, self.args, agent_archive, filename_map)

            aad.create_package_from_archive()

            # Gather variables in one dictionary for ease of generating the messages below
            msg_details = {"tar_name": aad.bundle_missing and aad.bundle_missing.tar_name or "None",
                       "package_id": aad.new_package["id"],
                       "acc_server": self.acc.server,
                        "warnings": aad.warnings}

            print("""
##############################################################################

Package id %(package_id)s created with %(warnings)d warnings.
""" % (msg_details))

            if self.args.download:
                aad.new_package.download(".", self.args.format)

            if aad.bundle_missing:
                print("""
Note, a new bundle has been created *locally* for unknown content:

  %(tar_name)s

Please review this bundle file, and if you would like to include it in your
package, first upload the bundle and then add the bundle to the package,
like this:

  bundles.py upload '%(tar_name)s' # <-- this will print the new bundle ID
  packages.py modify --add NEW_BUNDLE_ID_FROM_BUNDLE_UPLOAD %(package_id)d

Or alternatively, upload the bundle and re-run this script.

""" % msg_details)

            print('''
You can download the package by running:

  packages.py download %(package_id)d

Alternatively you can view/modify/download the package within ACC here:

  %(acc_server)s/#/packages?id=%(package_id)s

''' % msg_details)

if __name__ == "__main__":
    App().run()
