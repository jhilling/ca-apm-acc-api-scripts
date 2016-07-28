#!/usr/bin/env python

from __future__ import print_function

import pyacc

from pyacc import safe


class App(pyacc.AccCommandLineApp):
    """
    Bundle information.  Bundles are small pieces of Agent which are
    combined together to make a complete APM Agent Package which can then
    be downloaded and deployed (see packages.py)
    """
    def build_arg_parser(self):
        """
        Add some more args to the standard set
        """
        super(App, self).build_arg_parser()

        self.parser.add_argument('-v', '--verbose', action='store_true', help="be more verbose")

        subparsers = self.parser.add_subparsers(help="bundle sub-command", dest="command")

        list_parser = subparsers.add_parser("list")  # aliases=['ls'] only works with python 3
        list_parser.add_argument('--all', action='store_true', help="include all versions of bundles")
        list_parser.add_argument('bundle_ids', metavar='BUNDLE_ID', nargs='*', type=str,
                                 help='Query the given bundle ids')

        download_parser = subparsers.add_parser("download")
        download_parser.add_argument('bundle_ids', metavar='BUNDLE_ID', nargs='*', type=str,
                                 help='Download the given bundle ids')

        upload_parser = subparsers.add_parser("upload")
        upload_parser.add_argument('bundles', metavar='FILE', nargs='*', type=str,
                                 help='Upload the given bundle archives')

        delete_parser = subparsers.add_parser("delete")
        delete_parser.add_argument('bundle_ids', metavar='BUNDLE_ID', nargs='+', type=str,
                                   help='bundle ids')

    def download(self):
        for bundle in self._get_bundles():
            filename = bundle.download(".")

    def upload(self):
        for filename in self.args.bundles:
            bundle = self.acc.upload_bundle(filename)
            print("\t".join([str(bundle["id"]), bundle["name"], bundle["version"]]))

    def list(self):
        for bundle in self._get_bundles():

            if self.args.verbose:
                bundle["id"]
                print(bundle)
            else:
                # Print the bundle details
                print("\t".join([
                    str(bundle["id"]),
                    safe(bundle["name"]),
                    safe(bundle["version"]),
                    # safe(bundle["displayName"]),
                    # safe(bundle["description"]),
                    # safe(bundle["compatibility"]),
                    # safe(bundle["excludes"]),
                    # safe(bundle["facets"]),
                    # safe(bundle["installInstructions"]),
                    # safe(bundle["path"]),
                    # safe(bundle["dependencies"]),
                ]))

    def delete(self):
        for bundle_id in self.args.bundle_ids:
            print("Deleting bundle", bundle_id)
            b = self.acc.bundle(bundle_id)
            b.delete()

    def _get_bundles(self):
        if self.args.bundle_ids:
            # Create a list of Bundle objects initialized with the bundle id.
            # The data will be fetched from the Config Server when the object
            # is queried (e.g. "bundle["xxx"])
            bundles = self.acc.bundles_many(self.args.bundle_ids)
        else:
            # This will fetch all bundles (a page a time)
            bundles = self.acc.bundles(size=200)

        return bundles

    def main(self):

        # Route users command to the handler
        cmd = {
            "list": self.list,
            "delete": self.delete,
            # "create": self.create,
            "upload": self.upload,
            "download": self.download,
        }[self.args.command]
        cmd()

if __name__ == "__main__":
    App().run()
