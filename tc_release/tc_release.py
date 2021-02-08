import argparse
import contextlib
import fnmatch
import getpass
import logging
import os
import os.path
import re
import shutil
import stat
import subprocess
import sys
import uuid

from lxml import etree

logger = logging.getLogger(__name__)

# Platform-specific setup
if 'win' in sys.platform:
    # Set up git env variables
    username = getpass.getuser()
    git_app_data = 'C:\\Users\\{user}\\AppData\\Local\\Programs\\Git\\'
    local_git_install = git_app_data.format(user=username)

    if os.path.exists(local_git_install):
        os.environ['GIT_PYTHON_GIT_EXECUTABLE'] = (local_git_install +
                                                   'mingw64\\bin\\git.exe')
        os.environ['GIT_SSH'] = local_git_install + 'usr\\bin\\ssh.exe'

    # Follow windows standard for working directory
    dirname = '~tc-release-tmp'
else:
    # Follow linux standard for working directory
    dirname = '.tc-release-tmp'

# Late import, needs to be after the above on windows
from git import Repo  # noqa isort:skip

working_dir = os.path.join(os.getcwd(), dirname)

template_file = os.path.join(os.path.dirname(__file__), 'tcgvl.txt')
with open(template_file, 'r') as fd:
    GlobalVersion_TcGVL = fd.read()


def parse_args():
    parser = argparse.ArgumentParser(description=('Properly tags/version '
                                                  'your TC project with GIT'))
    parser.add_argument('version_string', metavar='VERSION NUMBER', type=str,
                        help='Version number must be vMAJOR.MINOR.BUGFIX')
    parser.add_argument('repo_url', type=str,
                        help='URL or path to the repo (for cloning)')
    parser.add_argument('--plcproj', default='',
                        help=('If multiple PLC projects in the repo, specify '
                              'which one to set the version number on.'))
    parser.add_argument('--deploy', action='store_true',
                        help=('Also make and deploy the IOC if applicable.'))
    parser.add_argument('--deploy-path', type=str,
                        help=('Specify a deploy directory for the IOC. '
                              'For example, --deploy-path /my/home/folder '
                              'will create a deployment at '
                              '/my/home/folder/repo_name/version_string. '
                              'If omitted, we will use '
                              '$EPICS_SITE_TOP/ioc/hutch, where hutch is '
                              'guessed based on the existing folders in '
                              '$EPICS_SITE_TOP/ioc and the repository name. '
                              'If $EPICS_SITE_TOP is unset, the default is '
                              'The SLAC LCLS PCDS directory, '
                              '/cds/group/pcds/epics.'))
    parser.add_argument('--dry-run', action='store_true',
                        help=('Run without pushing back to the repo and '
                              'without cleaning up the checkout directory.'))
    return parser.parse_args()


def find(pattern, path):
    result = []
    for root, dirs, files in os.walk(path):
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                result.append(os.path.join(root, name))
    return result


# Workaround for Windows file lock issues
def remove_readonly(func, path, _):
    "Clear the readonly bit and reattempt the removal"
    os.chmod(path, stat.S_IWRITE)
    func(path)


@contextlib.contextmanager
def pushd(new_dir):
    previous_dir = os.getcwd()
    os.chdir(new_dir)
    yield
    os.chdir(previous_dir)


def find_makefiles(directory):
    """
    Find the subdirectories that contain Makefiles.

    This will do a depth-first walk of the directory tree, accumulating a
    directory name whenever a file named "Makefile" is found, and pruning the
    search path at these directories to avoid double-counting any
    sub-Makefiles.

    An assumption is made that a Makefile will, in itself, trigger any
    Makefiles in subdirectories from that point in the directory tree.

    Parameters
    ----------
    directory : str
        The parent directory to use as the starting point for the depth-first
        search.

    Returns
    -------
    make_dirs : list of str
        The highest-level directories that contain files named "Makefile".
    """
    walker = os.walk(directory)
    make_dirs = []

    # Find the first Makefile in every branch of the directory tree
    for dirpath, dirnames, filenames in walker:
        if 'Makefile' in filenames:
            make_dirs.append(dirpath)
            # Stop searching this branch if we found a Makefile
            dirnames.clear()
            continue
        # Skip the version control directory
        try:
            dirnames.remove('.git')
        except ValueError:
            pass

    return make_dirs


def deploy(repo_url, tag, directory, dry_run):
    """
    Clone a repo to a specific directory at a specific tag, then build the IOC

    This will find the highest-level Makefile in the directory tree, then
    shell out to call 'make'.

    If more than one such file exists, we will make all of them.

    Parameters
    ----------
    repo_url : str
        The repo specification to clone from.
        For example: git@github.com:pcdshub/tc_release.git

    tag : str
        The tag to deploy, e.g. v1.0.0

    directory : str
        The parent directory to deploy to. We will automatically create a
        subdirectory with the name of the tag.
        For example: directory=/cds/group/epics/ioc/kfe/my_plc
        would deploy to /cds/group/epics/ioc/kfe/my_plc/v1.0.0

    dry_run : bool
        If True, don't actually do anything.
        If False, do things.
    """
    # Clone the repo
    deploy_dir = os.path.join(directory, tag)
    print(f'Deploying to {deploy_dir}')
    if not dry_run:
        Repo.clone_from(repo_url, deploy_dir, depth=1, branch=tag)

    # Find the makefiles
    if not dry_run:
        make_dirs = find_makefiles(deploy_dir)

    # Make all the makefiles
    if not dry_run:
        for make_dir in make_dirs:
            with pushd(make_dir):
                subprocess.run('make')


def make_deploy(args):
    if not args.deploy:
        return
    repo_url = args.repo_url
    tag = args.version_string
    dry_run = args.dry_run
    repo_name = os.path.split(repo_url)[-1].replace('.git', '')

    if args.deploy_path:
        deploy_path = args.deploy_path
    else:
        epics_site_top = os.environ.get('EPICS_SITE_TOP',
                                        '/cds/group/pcds/epics')
        ioc_dir = os.path.join(epics_site_top, 'ioc')
        categories = next(os.walk(ioc_dir))[1]
        repo_parts = repo_name.split('-')

        correct_category = None
        for cat in categories:
            if cat in repo_parts:
                correct_category = cat
                break

        if correct_category is None:
            raise RuntimeError('Can not determine where to deploy IOC')

        deploy_path = os.path.join(ioc_dir, correct_category)

    if not os.path.isdir(deploy_path):
        print(f'{deploy_path} does not exist! '
              'Verify you used a valid path!')

    repo_deploy_path = os.path.join(deploy_path, repo_name)

    if not args.dry_run:
        try:
            os.mkdir(repo_deploy_path)
        except FileExistsError:
            pass

    print(f'Deploying {repo_name} to {repo_deploy_path} at {tag}')
    deploy(repo_url, tag, repo_deploy_path, dry_run)


def make_release(args):
    # Create a temp directory, and clone the repo, and check out master

    logger.info('Creating working directory: %s', working_dir)
    logger.info('Initializing bare git repo, establishing remote, and '
                'checking out master')
    repo = Repo.init(working_dir)
    logger.info('Adding remote')
    logger.debug('Working directory: %s, Repo: %s',
                 working_dir, args.repo_url)
    origin = repo.create_remote('origin', str(args.repo_url))

    if not origin.exists():
        raise RuntimeError('Repo URL does not exist!')

    origin.fetch()

    repo.create_head('master', origin.refs.master)

    repo.heads.master.checkout()

    repo.heads.master.set_tracking_branch(origin.refs.master)

    if args.version_string in (tag.name for tag in repo.tags):
        print(f'Tag {args.version_string} already exists, skipping')
        repo.close()
        return

    # Check format of version_number
    projectVersion_pattern = re.compile(r'v([\d.]+)')
    version_string = re.search(projectVersion_pattern,
                               args.version_string).group(1)
    if not version_string:
        print('Error, version string does not match format "vX.X.X"')
        exit(1)

    # Inject version_number into .tcproj
    # Find .tcproj

    logger.info('Looking for .plcproj')
    plcproj_path = find('*.plcproj', working_dir)

    if not len(plcproj_path):
        print('Error, did not find .plcproj file.')
        exit(1)
    elif args.plcproj:
        for i, proj in enumerate(plcproj_path):
            if args.plcproj == os.path.split(proj)[1].split('.')[0]:
                plcproj_file = plcproj_path[i]
                break
        else:
            print(('Error, did not find specified file '
                   '{}.plcproj'.format(args.plcproj)))
            exit(1)
    elif len(plcproj_path) > 1:
        print('Error, found multiple .plcproj files.')
        exit(1)
    else:
        plcproj_file = plcproj_path[0]

    # Getting our xml structure to work with
    logger.info('Parsing plcproj')
    plcproj_tree = etree.parse(plcproj_file)

    plcproj_root = plcproj_tree.getroot()

    nsmap = plcproj_root.nsmap

    # Getting the ProjectVersion, Company, Author and Title tags
    projectVersion_tag = plcproj_root.find('.//ProjectVersion', nsmap)
    # company_tag and author_tag are currently unused
    # company_tag = plcproj_root.find('.//Company', nsmap)
    # author_tag = plcproj_root.find('.//Author', nsmap)
    title_tag = plcproj_root.find('.//Title', nsmap)

    if None in (projectVersion_tag, title_tag):
        err = ('Did not find a plc project version tag or a title tag! '
               'Did you forgot to set the plc project version to 0.0.0 '
               'or select an appropriate project title in TwinCAT?')
        repo.close()
        raise RuntimeError(err)

    logger.info('Updating plcproj with version number: %s', version_string)
    # Adding version string to plcproj

    projectVersion_tag.text = version_string

    # To make this verson number available in the PLC runtime,
    # we must add the Version/Global_Version.TcGVL to the project
    # Whether one exists or not, doesn't matter. We always just create one.
    # To start, we need the TC version of the project
    # and the current TcPlcObject version.
    # We can get this info for scanning for a .TcPOU in the project files
    # (there has to be at least one), and extracting these tags

    logger.info('Creating Global_Version.TcGVL')
    pouFiles = find('*.TcPOU', working_dir)
    pouTree = etree.parse(pouFiles[0])
    pouRoot = pouTree.getroot()

    TcPlcObject_PRODUCT_VERSION = pouRoot.get('ProductVersion')
    TcPlcObject_VERSION = pouRoot.get('Version')

    GlobalVersion_TcGVL_root = etree.XML(GlobalVersion_TcGVL)
    GlobalVersion_TcGVL_tree = etree.ElementTree(
        element=GlobalVersion_TcGVL_root)

    GlobalVersion_TcGVL_attributes = GlobalVersion_TcGVL_root.attrib

    GlobalVersion_TcGVL_attributes['ProductVersion'] = (
        TcPlcObject_PRODUCT_VERSION)
    GlobalVersion_TcGVL_attributes['Version'] = TcPlcObject_VERSION

    # We should also change the GUID, just to be nice.
    gvl_attrib = GlobalVersion_TcGVL_root.find('.//GVL').attrib

    try:
        gvl_attrib['Id'] = str(uuid.uuid4())
    except KeyError:
        print('Error, could not find Id attribute in GVL tag...')
        exit(1)

    # Now we modify the title and version numbers
    declaration = GlobalVersion_TcGVL_root.find('.//Declaration')
    declaration_text = declaration.text

    split_version_string = version_string.split('.')
    major = split_version_string[0]
    minor = split_version_string[1]
    build = split_version_string[2]
    if len(split_version_string) > 3:
        revision = split_version_string[4]
    else:
        revision = '0'

    pattern = re.compile(r"stLibVersion_(?P<title>.+)\s?:\s?ST_LibVersion.+"
                         r"iMajor.+(?P<major>\d+).+iMinor.+(?P<minor>\d+).+"
                         r"iBuild.+(?P<build>\d+).*iRevision.+"
                         r"(?P<revision>\d+).+sVersion.+"
                         r"'(?P<version_string>.+)'.+;")
    fmt = ("stLibVersion_{title} : ST_LibVersion := "
           "(iMajor := {iMajor}, iMinor := {iMinor}, "
           "iBuild := {iBuild}, iRevision := {iRevision}, "
           "sVersion := '{sVersion}');")
    replacement_string = fmt.format(
        iMajor=major,
        iMinor=minor,
        iBuild=build,
        iRevision=revision,
        sVersion=version_string,
        title=str(title_tag.text).replace(' ', '_').replace('-', '_')
    )

    declaration.text = etree.CDATA(pattern.sub(replacement_string,
                                               declaration_text))
    # Note, using the declaration.text not _ to write it back
    # Also note the use of the CDATA wrapper, this is stripped by .text,
    # so we need to restore it.

    # Add this file to the project by adding a Version/ folder and
    # Global_Version.TcGVL file and linking in the .tcproj

    # Creating file and folder
    logger.info('Writing Global_Version.TcGVL to file')
    version_dir = os.path.join(os.path.dirname(plcproj_file), 'Version')
    os.makedirs(version_dir, exist_ok=True)
    GV_TcGVL_file = os.path.join(version_dir, 'Global_Version.TcGVL')

    GlobalVersion_TcGVL_tree.write(GV_TcGVL_file, encoding='utf-8',
                                   xml_declaration=True)
    logger.info('File created successfully: %s', GV_TcGVL_file)

    # Linking in the .plcproj
    logger.info('Linking Global_Version.TcGVL into plcproj')

    # Check if Version folder is already linked, if not add it
    if not plcproj_root.find(".//ItemGroup/Folder[@Include='Version']", nsmap):
        folder = plcproj_root.find('.//ItemGroup/Folder', nsmap)
        folder.addnext(etree.XML('<Folder Include="Version" />'))

    # Check if Compile Include if not add it (it better already be there)
    ver_comp = r'.//ItemGroup/Compile[@Include="Version\Global_Version.TcGVL"]'
    if not plcproj_root.find(ver_comp, nsmap):
        compile_include = plcproj_root.find('.//ItemGroup/Compile', nsmap)
        compile_include.addnext(etree.XML(r'''
        <Compile Include="Version\Global_Version.TcGVL">
            <SubType>Code</SubType>
        </Compile>
        '''))

    # Writing new plcproj to disk
    logger.info('Writing updated .plcproj to file')
    plcproj_tree.write(plcproj_file, encoding='utf-8', xml_declaration=True)
    logger.info('.plc project has been updated')

    # Commit changes
    repoIndex = repo.index
    repoIndex.add([GV_TcGVL_file])
    repoIndex.add([plcproj_file])
    repoIndex.write()

    logger.info('Committing changes')
    commit_message = "Tagging version {version}".format(
        version=args.version_string)
    repoIndex.commit(commit_message, skip_hooks=True)

    # Tag this commit
    repo.create_tag(args.version_string, ref='HEAD', message=commit_message)

    # Push commit
    # --tags is not ideal, but since this is the only tag we're creating
    # in this temp area, it should be safe
    if args.dry_run:
        pushStatus = None
    else:
        pushStatus = origin.push(tags=True)
        logger.info('Push complete')

    repo.close()
    return pushStatus


def _main(args):
    make_release(args)
    make_deploy(args)


def main():
    logging.basicConfig(level=logging.DEBUG)
    args = parse_args()
    try:
        _main(args)
    finally:
        if not args.dry_run:
            logger.info('Cleaning up')
            shutil.rmtree(working_dir, onerror=remove_readonly)


if __name__ == "__main__":
    main()
