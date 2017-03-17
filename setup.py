from setuptools import setup, find_packages

long_description = open('README.md').read()

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name='tinymongo',
    packages=find_packages(),
    version='0.1.7.dev0',
    description='A flat file drop in replacement for mongodb.  Requires Tinydb',
    author='Stephen Chapman, Jason Jones',
    author_email='schapman1974@gmail.com',
    url='https://github.com/schapman1974/tinymongo',
    download_url='https://github.com/schapman1974/tinymongo/archive/master.zip',
    keywords=['mongodb', 'drop-in', 'database', 'tinydb'],
    long_description = long_description,
    classifiers=[],
    install_requires=requirements
)
