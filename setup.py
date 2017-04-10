import io
import os
from setuptools import setup, find_packages


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


with open('requirements.txt') as f:
    REQUIREMENTS = f.read().splitlines()


setup(
    name='tinymongo',
    packages=find_packages(),
    version='0.1.8.dev0',
    description='A flat file drop in replacement for mongodb.  Requires Tinydb',
    author='Stephen Chapman, Jason Jones',
    author_email='schapman1974@gmail.com',
    url='https://github.com/schapman1974/tinymongo',
    download_url='https://github.com/schapman1974/tinymongo/archive/master.zip',
    keywords=['mongodb', 'drop-in', 'database', 'tinydb'],
    long_description=parse_md_to_rst("README.md"),
    classifiers=[],
    package_data={'':['requirements.txt', 'README.md', 'LICENSE.txt']},
    install_requires=REQUIREMENTS
)
