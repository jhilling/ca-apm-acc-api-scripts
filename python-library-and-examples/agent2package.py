#!/usr/bin/env python


# Convert an agent installation to an equivalent package, including creating overrides.
# TODO optionally add extra files as a new bundle
# TODO does not handle dependencies.. i.e. if adding mqconnectors jms should also be added

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

from pyacc import safe


class BundleMappingException(Exception):
    pass


class AmbiguousBundleMappingException(BundleMappingException):
    pass


class BundleAlreadyIncludedException(BundleMappingException):
    def __init__(self, bundle):
        self.bundle = bundle


class NoAvailableBundleMappingException(BundleMappingException):
    pass


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
        self.parser.add_argument('--em-host', action='store', help="EM host name", default="http://em.ca.com:5001")
        self.parser.add_argument('--agent-version', action='store', help="agent version", default="10.2")
        self.parser.add_argument('--format', action='store',
                                 help='write files in the given format. "archive" means zip for windows packages, tar.gz for unix packages',
                                 default="archive", choices=["zip", "tar", "tar.gz", "archive"])

        self.parser.add_argument('-d', '--download', action='store_true', help="Download package after creating it")

        self.parser.add_argument('agent', metavar='AGENT', nargs='*', type=str, help='Agent Package')

    def fetch_bundles(self):
        bundle_files = []
        for bundle in self.acc.bundles(size=200):
            print("\t%s" % bundle["displayName"])
            if self.args.verbose:
                bundle["id"]
                print(bundle)

            temp_dir = "%s/bundle_temp_%s" % (tempfile.gettempdir(), hashlib.sha1(self.acc.server).hexdigest())
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            bundle.filename = bundle.download(directory=temp_dir)
            bundle_files.append(bundle)
        return bundle_files

    def index_bundles(self, bundle_files):
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

    def create_initial_package(self,
                               name,
                               os,
                               appserver,
                               em_host,
                               process_display_name,
                               agent_version,
                               comment):

        # Create draft package. This enables us to get the required/compatible/includes list of bundles
        new_package = self.acc.package_create(name=name,
                                              os=os,
                                              appserver=appserver,
                                              em_host=em_host,
                                              agent_version=agent_version,
                                              process_display_name=process_display_name,
                                              comment=comment,
                                              draft="true")

        print(new_package)

        return new_package

    def get_compatible_bundles(self, new_package):

        print("\nThese are the compatible bundles for the empty package:")
        compatible_bundles = {}
        for bundle in new_package.compatible_bundles():
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

    def get_required_bundles(self, new_package):

        print("\nThese are the required bundles:")
        required_bundles = {}
        for bundle in new_package.required_bundles():
            print("\t%s:%s" % (bundle["name"], bundle["version"]))
            required_bundles[bundle["name"]] = bundle

        return required_bundles

    def archive_entries(self, archive):
        """Loop over each file in an archive"""

        with tarfile.open(archive) as atf:
            for ti in atf:
                if ti.isdir():
                    pass
                else:
                    yield atf, ti

    def choose_bundle(self, entries, compatible_bundles, included_bundles):

        candidate = None

        for bundle in entries:
            exist = included_bundles.get(bundle["name"])
            if exist:
                # if something is already providing this file, stop searching
                raise BundleAlreadyIncludedException(bundle)
            else:
                compat = compatible_bundles.get(bundle["name"])

                if compat and bundle["version"] == compat["version"]:

                    if (candidate):
                        raise AmbiguousBundleMappingException("Have multiple compatible bundles which could provide that file or property")
                    else:
                        candidate = bundle

        if not candidate:
            raise NoAvailableBundleMappingException()

        return candidate

    def build_bundle_property_map(self, compatible_bundles):
        # Now need to look at the profile and potentially add bundles for those.
        # Will also need to worry about override values.

        property_bundle_map = {}

        print("\nReading properties for bundles:")
        for bundle in compatible_bundles:
            bundle.profile_property_map = {}

            print("\t%s:%s" % (bundle["name"], bundle["version"]))
            profile = bundle.profile()

            for prop in profile["properties"] or []:
                if not prop["hidden"]:
                    # print(prop)
                    bundle_entry = property_bundle_map.setdefault(prop["name"], {})
                    bundle_entry[bundle["name"]] = bundle

                    # save the property map on the bundle
                    bundle.profile_property_map[prop["name"]] = prop["value"] or ""

        return property_bundle_map

    def create_package_from_archive(self, agent_archive, filename_map):

        print("\nCreating empty package:")

        v2 = ".".join(self.args.agent_version.split(".")[0:2])

        new_package = self.create_initial_package(name=os.path.basename(agent_archive),
                                                  os=self.args.os,
                                                  appserver=self.args.appserver,
                                                  em_host=self.args.em_host,
                                                  process_display_name="process display name",
                                                  agent_version=v2,
                                                  comment="Package generated from Agent archive " +
                                                          os.path.basename(agent_archive))

        compatible_bundles = self.get_compatible_bundles(new_package)

        included_bundles = self.get_required_bundles(new_package)

        property_map = self.build_bundle_property_map(compatible_bundles.values())

        property_bundles = {}

        properties_from_archive = {}

        # TODO would life be easier if the filemap only consisted of compatible bundles (i.e. pre-filtered)?

        bundle_missing = None

        print("\nAnalyzing Agent Package: %s" % agent_archive)
        included_filename_map = {}
        for atf, ti in self.archive_entries(agent_archive):
            entries = filename_map.get(ti.name)

            if not entries:
                print("\t%s : WARNING: No bundle mapping" % (ti.name))

                if not bundle_missing:
                    name = "%s-unknown-files" % os.path.splitext(os.path.basename(agent_archive))[0]
                    bundle_missing = bundle_builder.BundleBuilder(name, force_overwrite_existing=True)

                bundle_missing.add_tarinfo_entry(atf, ti)
            else:
                print("\t%s : Provided by: %s" % (ti.name, ["%s:%s" % (x["name"], x["version"]) for x in entries or []]))
                included_filename_map[ti.name] = entries

            if ti.name.endswith(".profile"):
                print("\tFound profile:", ti.name)

                # parse that, look up the properties, etc
                for hidden, name, value in self.extract_properties(atf.extractfile(ti)):

                    if hidden:
                        print("Skip hidden property %s" % name)
                    else:
                        # Save the original property
                        properties_from_archive[name] = value

                        print("\t\tSearching for property: %s(=%s)" % (name, value))

                        bundle_map = property_map.get(name)

                        if not bundle_map:
                            print("\t\t\tCould not find the property. This will be added as an override.")
                        else:
                            print("\t\t\tFound property %s in %d bundles (%s)" % (name, len(bundle_map),
                                ["%s:%s" % (bundle["name"], bundle["version"]) for bundle in bundle_map.itervalues()]))

                            property_bundles[name] = bundle_map.values()

        if bundle_missing:
            bundle_missing.close()

        # Pick bundles for the files we have
        self.resolve_bundles(["file", "files"], included_filename_map, compatible_bundles, included_bundles)

        # Pick bundles for the properties we have
        property_to_bundle_map = self.resolve_bundles(["property", "properties"], property_bundles, compatible_bundles, included_bundles)

        # Resolve remaining dependencies
        self.resolve_dependencies(included_bundles, compatible_bundles)

        master = new_package["bundleOverrides"]
        override_count = self.create_overrides(master, properties_from_archive, property_to_bundle_map)

        override_count += self.create_overrides2(master, included_bundles, properties_from_archive)

        # Now add the bundles we selected to the package
        self.add_bundles_to_package(new_package, included_bundles.values())

        # TODO need to check if our bundles are setting properties that were not previously being set and hide them
        # We do have the properties don't we?


        # Add the overrides TODO really want to do this as part of the same patch, rather than 2 patches

        if override_count > 0:
            print("\nAdding %d overrides" % override_count)
            new_package.add_overrides(master)

        return bundle_missing, new_package

    def split_property(self, prop):
        prop_split = App.val_re.split(prop)
        # print("prop_split", prop_split)

        hidden = prop_split[1] == "#"
        name = prop_split[2].strip()
        value = prop_split[3].strip()
        return hidden, name, value

    def extract_properties(self, fileobj):
        for raw in fileobj:
            line = raw.strip()
            if not line or line[0] == "#":
                continue
            try:
                hidden, name, value = self.split_property(line)
                yield hidden, name, value
            except IndexError as e:
                print("Failed to process", line.strip())

    def create_overrides(self, master, properties_from_archive, property_to_bundle_map):

        override_count = 0

        # Need to check what the value in the profile and the bundle are.
        # If they are != then we need to create an override
        for property, value in properties_from_archive.iteritems():

            # print("\tFrom archive: %s=%s" % (property, value))

            # What is the value in the bundle?
            bundle = property_to_bundle_map.get(property)

            if not bundle:
                print("\tProperty %s is not fulfilled by any bundle, need to create an override to create the property" % property)

                overrides = master.setdefault("java-agent", {"preamble": None, "properties":[]})

                prop_dic = {"description": None,
                            "hidden": False,
                            "name":  property,
                            "value": value,
                            "userKey": "+"}

                overrides["properties"].append(prop_dic)

                override_count += 1
            else:

                bundle_value = bundle.profile_property_map[property]

                print("\tProperty %s=%s is fulfilled by bundle %s:%s as %s=%s" % (property, value, bundle["name"], bundle["version"], property, bundle_value))

                if value != bundle_value:
                    print("\t\tValues differ - will create an override: %s=%s\n" % (property, value))

                    overrides = master.setdefault(bundle["name"], {"preamble": None, "properties":[]})

                    prop_dic = {"description": None,
                                "hidden": False,
                                "name":  property,
                                "value": value,
                                "userKey": None}

                    overrides["properties"].append(prop_dic)

                    override_count += 1

        return override_count

    def create_overrides2(self, master, included_bundles, properties_from_archive):
        print("\nChecking for extra properties in included bundles\n")

        override_count = 0

        for bundle in included_bundles.values():

            print("Bundle %s:%s" % (bundle["name"], bundle["version"]))

            # TODO OPT we're feting the profile again. the bundle in the compatible_bundles list should alraedy have it in there.
            profile = bundle.profile()

            for prop in profile["properties"] or []:
                if not prop["hidden"]:

                    # What was the state of this property in the original agent package?

                    original_property = properties_from_archive.get(prop["name"])

                    if not original_property:
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

    def resolve_bundles(self, mapping_type, included_filename_map, compatible_bundles, included_bundles):

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
                    candidate = self.choose_bundle(entries, compatible_bundles, included_bundles)

                    # Add the matched bundle to the included bundles list
                    print("\tSelected bundle %s:%s for %s %s" % (candidate["name"], candidate["version"], mapping_type[0], filename_or_property))
                    included_bundles[candidate["name"]] = candidate

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

    def resolve_dependencies(self, bundles, compatible_bundles):
        print("\nResolve remaining dependencies for package\n")

        facets_included_map = {}
        for bundle in bundles.values():
            for facet in bundle["facets"]:
                facets_included_map.setdefault(facet, []).append(bundle)

        facets_compatible_map = {}
        for bundle in compatible_bundles.values():
            for facet in bundle["facets"]:
                facets_compatible_map.setdefault(facet, []).append(bundle)

        for bundle in bundles.values():
            self.resolve_dependency_recursive(facets_included_map, facets_compatible_map, bundle, bundles, compatible_bundles)

    def resolve_dependency_recursive(self, facets_included_map, facets_compatible_map, bundle, bundles, compatible_bundles):

        print("Bundle %s" % bundle["name"])

        for dep in bundle["dependencies"]:

            dep_implemented_by = facets_included_map.get(dep)

            if not dep_implemented_by:

                # Search compatible bundles for that one
                entries = facets_compatible_map.get(dep)

                candidate = self.choose_bundle(entries, compatible_bundles, bundles)

                print("\tNeed to additionally include %s:%s" % (candidate["name"], candidate["version"]))
                bundles[candidate["name"]] = candidate

                for facet in candidate["facets"]:
                    facets_included_map.setdefault(facet, []).append(candidate)

                # Now recurse for the one we added
                self.resolve_dependency_recursive(facets_included_map, facets_compatible_map, candidate, bundles, compatible_bundles)
            else:
                dep_implemented_by = [b["name"] for b in dep_implemented_by]
                print("\tdepends on: %s which is provided by: %s" % (dep, dep_implemented_by))

    def add_bundles_to_package(self, new_package, bundles):

        print("\nAdding %d bundles to package:" % len(bundles))
        for bundle in bundles:
            print("\t%s:%s" % (bundle["name"], bundle["version"]))

        new_package.add_bundles([b["id"] for b in bundles])

    def main(self):

        for agent_archive in self.args.agent:
            if not os.path.exists(agent_archive):
                print("ERROR: %s does not exist" % agent_archive)
                sys.exit(1)

        print("\nFetching bundles:")
        bundle_files = self.fetch_bundles()

        print("\nIndexing bundles:")
        filename_map = self.index_bundles(bundle_files)

        for agent_archive in self.args.agent:
            bundle_missing, new_package = self.create_package_from_archive(agent_archive, filename_map)

            msg_details = {"tar_name": bundle_missing and bundle_missing.tar_name or "None",
                       "package_id": new_package["id"],
                       "acc_server": self.acc.server}

            print("""
##############################################################################

Package id of created package is: %(package_id)s
""" % (msg_details))

            if self.args.download:
                new_package.download(".", self.args.format)

            if bundle_missing:
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
