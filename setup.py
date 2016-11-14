from setuptools import setup, find_packages

requirements = ['tinydb', 'pymongo']

setup(
    name='tinymongo',
    packages=find_packages(),
    version='0.1.2dev0',
    description='A flat file drop in replacement for mongodb.  Requires Tinydb, pymongo',
    author='Stephen Chapman',
    author_email='schapman1974@gmail.com',
    url='https://github.com/schapman1974/tinymongo',
    download_url='https://github.com/schapman1974/tinymongo/archive/master.zip',
    keywords=['mongodb', 'drop-in', 'database', 'tinydb'],
    classifiers=[],
    install_requires=requirements
)
