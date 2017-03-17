from setuptools import setup, find_packages

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
    classifiers=[],
    install_requires=requirements
)
