import io
import os
from setuptools import setup, find_packages
from setuptools.command.test import test as TestCommand

def read(*names, **kwargs):
    """Read a file."""
    return io.open(
        os.path.join(os.path.dirname(__file__), *names),
        encoding=kwargs.get('encoding', 'utf8')
    ).read()


def parse_md_to_rst(file):
    """Read Markdown file and convert to ReStructured Text."""
    try:
        from m2r import parse_from_file
        return parse_from_file(file).replace(
            "artwork/", "http://198.27.119.65/"
        )
    except ImportError:
        # m2r may not be installed in user environment
        return read(file)


class PyTest(TestCommand):
    """PyTest cmdclass hook for test-at-buildtime functionality

    http://doc.pytest.org/en/latest/goodpractices.html#manual-integration

    """
    user_options = [('pytest-args=', 'a', "Arguments to pass to pytest")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = [
            'tests/',
            '-rx'
        ]    #load defaults here

    def run_tests(self):
        import shlex
        #import here, cause outside the eggs aren't loaded
        import pytest
        pytest_commands = []
        try:    #read commandline
            pytest_commands = shlex.split(self.pytest_args)
        except AttributeError:  #use defaults
            pytest_commands = self.pytest_args
        errno = pytest.main(pytest_commands)
        exit(errno)

setup(
    name='tinymongo',
    packages=find_packages(),
    version='0.2.0',
    description='A flat file drop in replacement for mongodb.  Requires Tinydb',
    author='Stephen Chapman, Jason Jones',
    author_email='schapman1974@gmail.com',
    url='https://github.com/schapman1974/tinymongo',
    download_url='https://github.com/schapman1974/tinymongo/archive/master.zip',
    keywords=['mongodb', 'drop-in', 'database', 'tinydb'],
    long_description=parse_md_to_rst("README.md"),
    classifiers=[
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6'
    ],
    install_requires=[
        'tinydb>=3.2.1',
        'tinydb_serialization>=1.0.4'
    ],
    tests_require=[
        'pytest>=3.2.0',
        'py>=1.4.33'
    ],
    cmdclass={
        'test':PyTest
    }
)
