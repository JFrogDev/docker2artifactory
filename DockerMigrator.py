import argparse
import logging
import sys
import Queue
from migrator.Migrator import Migrator
from migrator.ArtifactoryDockerAccess import ArtifactoryDockerAccess
from migrator.DockerRegistryAccess import DockerRegistryAccess
from migrator.QuayAccess import QuayAccess
import os
import shutil
dir_path = os.path.dirname(os.path.realpath(__file__))

'''
    Entry point and argument parser for Docker to Artifactory migrator

    Supports:

     generic - Migrate from a generic, token based registry.
     quay - Migrate from a SaaS Quay registry.
     quayee - Migrate from Quay Enterprise.
'''


# Globals
NUM_OF_WORKERS = 2
MIN_NUM_OF_WORKERS = 1
MAX_NUM_OF_WORKERS = 16


def add_extra_args(parser):
    parser.add_argument('--ignore-certs', dest='ignore_cert', action='store_const', const=True, default=False,
                                help='Ignore any certificate errors from both source and destination')
    parser.add_argument('--overwrite', action='store_true', 
                                help='Overwrite existing image/tag on the destination')
    parser.add_argument('--num-of-workers', dest='workers', type=int, default=NUM_OF_WORKERS,
                                help='Number of worker threads. Defaults to %d.' % NUM_OF_WORKERS)
    parser.add_argument('-v', '--verbose', action='store_true', help='Make the operation more talkative')
    # Provide a predefined set of images to import
    parser.add_argument('--image-file', dest='image_file',
                                help='Limit the import to a set of images in the provided file. '
                                     'Format of new line separated file: \'<image-name>:<tag>\' OR '
                                     '\'<image-name>\' to import all tags of that repository.')


def add_art_access(parser):
    art_group = parser.add_argument_group('artifactory')
    art_group.add_argument('artifactory', help='The destination Artifactory URL')
    art_group.add_argument('username', help='The username to use for authentication to Artifactory')
    art_group.add_argument('password', help='The password to use for authentication to Artifactory')
    art_group.add_argument('repo', help='The docker repository')

# Sets up the argument parser for the application


def get_arg_parser():
    parser = argparse.ArgumentParser(prog='python DockerMigrator.py', description='Docker registry to Artifactory migrator.')

    # Generic Registry Parser
    subparsers = parser.add_subparsers(help='sub-command help')
    parser_generic = subparsers.add_parser('generic', help='A generic tool to migrate a single registry')
    # Source registry access
    source_group = parser_generic.add_argument_group('source')
    source_group.add_argument('source', help='The source registry URL')
    source_group.add_argument('--source-username', help='The username to use for authentication to the source')
    source_group.add_argument('--source-password', help='The password to use for authentication to the source')
    # Artifactory access
    add_art_access(parser_generic)
    # Extra options
    add_extra_args(parser_generic)
    parser_generic.set_defaults(func=generic_migration)

    # QUAY
    parser_quay = subparsers.add_parser('quay', help='A tool specifically for Quay SaaS')
    quay = parser_quay.add_argument_group('source')
    quay.add_argument('namespace', help='The username or organization to import repositories from')
    quay.add_argument('token', help='The OAuth2 Access Token')
    # Artifactory access
    add_art_access(parser_quay)
    # Extra options
    add_extra_args(parser_quay)
    parser_quay.set_defaults(func=quay_migration)

    # Quay enterprise
    parser_quay_ee = subparsers.add_parser('quayee', help='A tool specifically for Quay Enterprise')
    quay_ee = parser_quay_ee.add_argument_group('source')
    quay_ee.add_argument('source', help='The source registry URL')
    quay_ee.add_argument('--source-username', help='The super user username')
    quay_ee.add_argument('--source-password', help='The super user password')
    quay_ee.add_argument('--token', help='The OAuth2 Access Token')
    # Artifactory access
    add_art_access(parser_quay_ee)
    # Extra options
    add_extra_args(parser_quay_ee)
    parser_quay_ee.set_defaults(func=quay_ee_migration)
    return parser


'''
    Parse image file
      Returns two different lists, one with just image names and one with image/tag tuple.
      Example:

      Input file:
      image_name1
      image_name2,
      image_name3:tag1
      image_name4:tag2

      Result:
      [image_name1, image_name2,...], [(image_name3, tag1), (image_name4, tag2),...]
'''
def parse_image_file(file_path):
    image_names = []
    images = []
    try:
        with open(file_path) as f:
            content = f.readlines()
            for unprocessed_line in content:
                line = unprocessed_line.strip()
                if ':' in line:
                    name, tag = line.split(':')
                    if name and tag:
                        images.append((name, tag))
                elif len(line) > 0:
                    image_names.append(line)
        return image_names, images
    except Exception as ex:
        logging.error("Unable to read in image file '%s' due to %s" % (file_path, ex.message))
        return [], []

'''
    Generic migration for a V2 token based Docker registry
    @param args - The user provided arguments
    @param work_dir - The temporary work directory
'''
def generic_migration(args, work_dir):
    # Verify the more intricate argument requirements
    if bool(args.source_username) != bool(args.source_password):
        parser.error("--source-username and --source-password must both be provided or neither.")
    if args.workers < MIN_NUM_OF_WORKERS or args.workers > MAX_NUM_OF_WORKERS:
        parser.error("--num-of-workers must be between %d and %d." % (MIN_NUM_OF_WORKERS, MAX_NUM_OF_WORKERS))

    # Set up and verify the connection to the source registry
    source = DockerRegistryAccess(args.source, args.source_username, args.source_password, args.ignore_cert)
    if not source.verify_is_v2():
        sys.exit("The provided URL does not appear to be a valid V2 repository.")

    # Set up and verify the connection to Artifactory
    art_access = setup_art_access(args.artifactory, args.username, args.password, args.repo, args.ignore_cert)

    image_names = []
    q = Queue.Queue()
    # Build the list of image/tags
    # If the user provides a set of images, don't query the upstream
    if 'image_file' in args and args.image_file:
        image_names, images = parse_image_file(args.image_file)
        for image_name, tag in images:
            q.put_nowait((image_name, tag))
    else:
        logging.info("Requesting catalog from source registry.")
        image_names = source.get_catalog()
        if not image_names:
            print "Found no repositories."

    if image_names:
        print "Found %d repositories." % len(image_names)
        populate_tags(image_names, source, q)
    if not q.empty():
        # Perform the migration
        perform_migration(source, art_access, q, work_dir)
    else:
        print "Nothing to migrate."

'''
    Set up and verify the connection to Artifactory
    @param artifactory_url - The URL to the Artifactory instance
    @param username - The username to access Artifactory
    @param password - The password (API Key, encrypted password, token) to access Artifactory
    @param repo - The repo name
    @param ignore_cert - True if the certificate to this instance should be ignored
'''
def setup_art_access(artifactory_url, username, password, repo, ignore_cert):
    art_access = ArtifactoryDockerAccess(url=artifactory_url, username=username,
                                   password=password, repo=repo, ignore_cert=ignore_cert)
    if not art_access.is_valid():
        sys.exit("The provided Artifactory URL or credentials do not appear valid.")
    if not art_access.is_valid_version():
        sys.exit("The provided Artifactory instance is version %s but only 4.4.3+ is supported." %
                 art_access.get_version())
    if not art_access.is_valid_docker_repo():
        sys.exit("The repo %s does not appear to be a valid V2 Docker repository." % args.repo)

    return art_access

'''
    Finds and populates the tags for a set of image names
    @param image_names - A list of images names
    @param source - Access to the source registry
    @param q - The queue to populate with (image_name, tag) tuples
'''
def populate_tags(image_names, source, q):
    print "Populating set of image/tags..."
    for image_name in image_names:
        image_name = str(image_name)
        tags = source.get_tags(image_name)
        if tags:
            print "Found %d tags for repository %s." % (len(tags), image_name)
            for tag in tags:
                tag = str(tag)
                q.put_nowait((image_name, tag))


'''
    Perform the migration
    @param source - Access to the source registry
    @param art_access - Access to the Artifactory destination
    @param q - The queue of (image, tag) tuples that have to be migrated
    @param work_dir - The temporary working directory
'''
def perform_migration(source, art_access, q, work_dir):
    print "Performing migration for %d image/tags." % q.qsize()
    m = Migrator(source, art_access, q, args.workers, args.overwrite, work_dir)
    m.migrate()
    print "Migration finished."
    # Report any skipped images
    skipped_list = list(m.get_skipped_queue().queue)
    skipped_count = len(skipped_list)
    if skipped_list and skipped_count > 0:
        print "Skipped %d images because they already exist in Artifactory." % skipped_count
    # Report on any failures
    failure_list = list(m.get_failure_queue().queue)
    failure_count = len(failure_list)
    if failure_list and failure_count > 0:
        print "Failed to migrate the following %d images: " % failure_count
        for image, tag in failure_list:
            print "    %s/%s" % (image, tag)


def quay_migration(args, work_dir):
    # Set up and verify the connection to Artifactory
    art_access = setup_art_access(args.artifactory, args.username, args.password, args.repo, args.ignore_cert)

    q = Queue.Queue()

    # If the user provides a set of images, don't query the upstream
    if 'image_file' in args and args.image_file:
        image_names, images = parse_image_file(args.image_file)
        for image_name, tag in images:
            q.put_nowait((image_name, tag))
    else:
        quay = QuayAccess(args.namespace, args.token)
        image_names = quay.get_catalog()
        if not image_names:
            logging.error("Failed to retrieve catalog.")
    # Set up the token based connection to Quay
    source = DockerRegistryAccess("https://quay.io", "$oauthtoken", args.token, args.ignore_cert)
    if image_names:
        print "Found %d repositories." % len(image_names)
        populate_tags(image_names, source, q)
    if not q.empty():
        # Perform the migration
        perform_migration(source, art_access, q, work_dir)
    else:
        print "Nothing to migrate."

def quay_ee_migration(args, work_dir):
    # Verify arguments
    if bool(args.source_username) != bool(args.source_password):
        parser.error("--source-username and --source-password must both be provided or neither.")
    if bool(args.token) and bool(args.source_username):
        parser.error("The token and source username/password arguments are mutually exclusive.")
    if not(bool(args.token) or bool(args.source_username)):
        parser.error("The token or source username/password arguments must be specified.")
    if bool(args.token):
        # Transform the token into username/password
        args.source_username = "$oauthtoken"
        args.source_password = args.token
    generic_migration(args, work_dir)

def setup_logging(level):
    fmt = "%(asctime)s [%(threadName)s] [%(levelname)s]"
    fmt += " (%(name)s:%(lineno)d) - %(message)s"
    formatter = logging.Formatter(fmt)
    stdouth = logging.StreamHandler(sys.stdout)
    stdouth.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(level)
    logger.handlers = []
    logger.addHandler(stdouth)

if __name__ == '__main__':
    # Argument parsing
    logging.info("Parsing and verifying user provided arguments.")
    parser = get_arg_parser()
    args = parser.parse_args()

    # Set log level
    if args.verbose:
        setup_logging(logging.INFO)
    else:
        setup_logging(logging.WARN)

    # Create temp dir
    work_dir = os.path.join(dir_path, 'workdir')
    if not os.path.exists(work_dir):
        try:
            os.makedirs(work_dir)
        except:
            sys.exit("Failed to create work directory '%s'" % work_dir)

    # Calls the appropriate function based on user's selected operation
    args.func(args, work_dir)

    # Delete temp dir
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
