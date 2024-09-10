import sys
import shutil
import winshell
import pythoncom

from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Dict
from dataclasses import dataclass, field
from enum import Enum

import core.path_manager as Paths
import core.event_manager as Events
import core.config_manager as Config

from core.package_manager import Package, PackageMetadata

from core.utils.ini_handler import IniHandler, IniHandlerSettings


class SettingType(Enum):
    Constant = 'constant'
    Bool = 'bool'
    Map = 'map'


@dataclass
class ModelImporterEvents:

    @dataclass
    class StartGame:
        pass

    @dataclass
    class ValidateGameFolder:
        game_folder: str

    @dataclass
    class CreateShortcut:
        pass


@dataclass
class ModelImporterConfig:
    package_name: str = ''
    importer_folder: str = ''
    game_folder: str = ''
    launcher_theme: str = 'Default'
    overwrite_ini: bool = True
    run_pre_launch: str = ''
    run_pre_launch_signature: str = ''
    run_pre_launch_wait: bool = False
    run_post_load: str = ''
    run_post_load_signature: str = ''
    run_post_load_wait: bool = False
    d3dx_ini: Dict[
        str, Dict[str, Dict[str, Union[str, int, float, Dict[str, Union[str, int, float]]]]]
    ] = field(default_factory=lambda: {})

    @property
    def importer_path(self) -> Path:
        importer_path = Path(self.importer_folder)
        if importer_path.is_absolute():
            return importer_path
        else:
            return Paths.App.Root / importer_path

    @property
    def theme_path(self) -> Path:
        return Paths.App.Themes / self.launcher_theme


class ModelImporterPackage(Package):
    def __init__(self, metadata: PackageMetadata):
        super().__init__(metadata)
        self.backups_path = None

    def load(self):
        self.subscribe(Events.ModelImporter.StartGame, self.start_game)
        self.subscribe(Events.ModelImporter.ValidateGameFolder, lambda event: self.validate_game_path(event.game_folder))
        self.subscribe(Events.ModelImporter.CreateShortcut, lambda event: self.create_shortcut())
        super().load()
        try:
            game_path = self.validate_game_path(Config.Active.Importer.game_folder)
            self.validate_game_exe_path(game_path)
        except Exception as e:
            try:
                game_folder = self.autodetect_game_folder()
                game_path = self.validate_game_path(game_folder)
                self.validate_game_exe_path(game_path)
                Config.Active.Importer.game_folder = str(game_folder)
            except Exception as e:
                pass

    def unload(self):
        self.unsubscribe()
        super().unload()

    def install_latest_version(self, clean):
        Events.Fire(Events.PackageManager.InitializeInstallation())

        self.initialize_backup()
        d3dx_ini_path = Config.Active.Importer.importer_path / 'd3dx.ini'
        self.backup(d3dx_ini_path)

        self.move_contents(self.downloaded_asset_path, Config.Active.Importer.importer_path)

        if not Config.Active.Importer.overwrite_ini:
            self.restore(d3dx_ini_path)

    def get_game_exe_path(self, game_path: Path) -> Path:
        raise NotImplementedError

    def initialize_game_launch(self, game_path: Path):
        raise NotImplementedError

    def validate_game_path(self, game_folder) -> Path:
        game_path = Path(game_folder)
        if not game_path.is_absolute():
            raise ValueError(f'Game folder is not a valid path!')
        if not game_path.exists():
            raise ValueError(f'Game folder does not exist!')
        if not game_path.is_dir():
            raise ValueError(f'Game folder is not a directory!')
        return game_path

    def update_d3dx_ini(self):
        Events.Fire(Events.Application.StatusUpdate(status='Updating d3dx.ini...'))

        ini_path = Config.Active.Importer.importer_path / 'd3dx.ini'

        with open(ini_path, 'r') as f:
            ini = IniHandler(IniHandlerSettings(ignore_comments=False), f)

        self.set_default_ini_values(ini, 'core', SettingType.Constant)
        self.set_default_ini_values(ini, 'debug_logging', SettingType.Bool, Config.Active.Migoto.debug_logging)
        self.set_default_ini_values(ini, 'mute_warnings', SettingType.Bool, Config.Active.Migoto.mute_warnings)
        self.set_default_ini_values(ini, 'enable_hunting', SettingType.Bool, Config.Active.Migoto.enable_hunting)
        self.set_default_ini_values(ini, 'dump_shaders', SettingType.Bool, Config.Active.Migoto.dump_shaders)

        if ini.is_modified():
            with open(ini_path, 'w') as f:
                f.write(ini.to_string())

    def set_default_ini_values(self, ini: IniHandler, setting_name: str, setting_type: SettingType, setting_value=None):
        settings = Config.Active.Importer.d3dx_ini.get(setting_name, None)
        if settings is None:
            raise ValueError(f'Config is missing {setting_name} setting!')
        for section, options in settings.items():
            for option, values in options.items():

                key, value = None, None

                if setting_type == SettingType.Constant:
                    value = values
                elif setting_type == SettingType.Bool:
                    key = 'on' if setting_value else 'off'
                    value = values[key]
                elif setting_type == SettingType.Map:
                    key = setting_value
                    value = values[key]

                if value is None:
                    raise ValueError(f'Config is missing value for section `{section}` option `{option}` key `{key}')

                try:
                    ini.set_option(section, option, value)
                except Exception as e:
                    raise ValueError(f'Failed to set section {section} option {option} to {value}: {str(e)}') from e

    def validate_game_exe_path(self, game_path: Path) -> Path:
        raise NotImplementedError

    def start_game(self, event):
        # Ensure package integrity
        self.validate_package_files()
        # Write configured settings to main 3dmigoto ini file
        self.update_d3dx_ini()
        # Check if game location is properly configured
        try:
            game_path = self.validate_game_path(Config.Active.Importer.game_folder)
            game_exe_path = self.validate_game_exe_path(game_path)
        except Exception as e:
            try:
                game_folder = self.autodetect_game_folder()
                game_path = self.validate_game_path(game_folder)
                game_exe_path = self.validate_game_exe_path(game_path)
                Config.Active.Importer.game_folder = str(game_folder)
            except Exception as e:
                Events.Fire(Events.Application.OpenSettings())
                return

        # Execute initialization sequence of implemented importer
        self.initialize_game_launch(game_path)

        Events.Fire(Events.MigotoManager.StartAndInject(exe_path=game_exe_path))

    def autodetect_game_folder(self) -> Path:
        raise NotImplementedError

    def validate_package_files(self):
        ini_path = Config.Active.Importer.importer_path / 'd3dx.ini'
        if not ini_path.exists():
            user_requested_restore = Events.Call(Events.Application.ShowError(
                modal=True,
                confirm_text='Restore',
                cancel_text='Cancel',
                message=f'{Config.Launcher.active_importer} installation is damaged!\n'
                        f'Details: Missing critical file: {ini_path.name}!\n'
                        f'Would you like to restore {Config.Launcher.active_importer} automatically?',
            ))

            if not user_requested_restore:
                raise ValueError(f'Missing critical file: {ini_path.name}!')

            Events.Fire(Events.Application.Update(no_thread=True, force=True, reinstall=True, packages=[self.metadata.package_name]))

    def initialize_backup(self):
        backup_name = self.metadata.package_name + ' ' + datetime.now().strftime('%Y-%m-%d %H-%M-%S')
        self.backups_path = Paths.App.Backups / backup_name

    def backup(self, file_path: Path):
        if not file_path.exists():
            return
        Paths.verify_path(self.backups_path)
        shutil.copy2(file_path, self.backups_path / file_path.name)

    def restore(self, file_path: Path):
        if not file_path.exists():
            return
        shutil.copy2(self.backups_path / file_path.name, file_path)

    def create_shortcut(self):
        pythoncom.CoInitialize()
        with winshell.shortcut(str(Path(winshell.desktop()) / f'{Config.Launcher.active_importer} Quick Start.lnk')) as link:
            link.path = str(Path(sys.executable))
            link.description = f'Start game with {Config.Launcher.active_importer} and skip launcher load'
            link.working_directory = str(Paths.App.Root)
            link.arguments = f'--nogui --xxmi {Config.Launcher.active_importer}'
            link.icon_location = (str(Config.Active.Importer.theme_path / 'Shortcuts' / f'{Config.Launcher.active_importer}.ico'), 0)
