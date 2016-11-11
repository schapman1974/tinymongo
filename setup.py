from setuptools import setup, find_packages

requirements = ['tinydb']

setup(
    name='tinymongo',
    packages=find_packages(),
    version='0.1.1dev',
    description='A flat file drop in replacement for mongodb.  Requires Tinydb',
    author='Stephen Chapman',
    author_email='schapman1974@gmail.com',
    url='https://github.com/schapman1974/tinymongo',
    download_url='https://github.com/schapman1974/tinymongo/archive/master.zip',
    keywords=['mongodb', 'drop-in', 'database', 'tinydb'],
    classifiers=[],
    install_requires=requirements
)
