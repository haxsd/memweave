from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as SetuptoolsBuildPy


class CleanBuildPy(SetuptoolsBuildPy):
    def run(self) -> None:
        build_lib = Path(self.build_lib)
        if build_lib.is_dir():
            shutil.rmtree(build_lib)
        super().run()


setup(cmdclass={"build_py": CleanBuildPy})
