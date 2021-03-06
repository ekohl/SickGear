# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of SickGear.
#
# SickGear is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickGear is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickGear.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement

import os.path
import re
import sqlite3
import time
import threading

import sickbeard
from sickbeard import encodingKludge as ek
from sickbeard import logger
from sickbeard.exceptions import ex
import helpers


db_lock = threading.Lock()


def dbFilename(filename='sickbeard.db', suffix=None):
    """
    @param filename: The sqlite database filename to use. If not specified,
                     will be made to be sickbeard.db
    @param suffix: The suffix to append to the filename. A '.' will be added
                   automatically, i.e. suffix='v0' will make dbfile.db.v0
    @return: the correct location of the database file.
    """
    if suffix:
        filename = '%s.%s' % (filename, suffix)
    return ek.ek(os.path.join, sickbeard.DATA_DIR, filename)


def mass_upsert_sql(table_name, value_dict, key_dict, sanitise=True):
    """
    use with cl.extend(mass_upsert_sql(tableName, valueDict, keyDict))

    :param table_name: table name
    :param value_dict: dict of values to be set {'table_fieldname': value}
    :param key_dict: dict of restrains for update {'table_fieldname': value}
    :param sanitise: True to remove k, v pairs in keyDict from valueDict as they must not exist in both.
    This option has a performance hit so it's best to remove key_dict keys from value_dict and set this False instead.
    :type sanitise: Boolean
    :return: list of 2 sql command
    """
    cl = []

    gen_params = (lambda my_dict: [x + ' = ?' for x in my_dict.keys()])

    # sanity: remove k, v pairs in keyDict from valueDict
    if sanitise:
        value_dict = dict(filter(lambda (k, _): k not in key_dict.keys(), value_dict.items()))

    cl.append(['UPDATE [%s] SET %s WHERE %s' %
               (table_name, ', '.join(gen_params(value_dict)), ' AND '.join(gen_params(key_dict))),
               value_dict.values() + key_dict.values()])

    cl.append(['INSERT INTO [' + table_name + '] (' +
               ', '.join(["'%s'" % ('%s' % v).replace("'", "''") for v in value_dict.keys() + key_dict.keys()]) + ')' +
               ' SELECT ' +
               ', '.join(["'%s'" % ('%s' % v).replace("'", "''") for v in value_dict.values() + key_dict.values()]) +
               ' WHERE changes() = 0'])
    return cl


class DBConnection(object):
    def __init__(self, filename='sickbeard.db', suffix=None, row_type=None):

        db_src = dbFilename(filename)
        if not os.path.isfile(db_src):
            db_alt = dbFilename('sickrage.db')
            if os.path.isfile(db_alt):
                helpers.copyFile(db_alt, db_src)

        self.filename = filename
        self.connection = sqlite3.connect(db_src, 20)

        if row_type == 'dict':
            self.connection.row_factory = self._dict_factory
        else:
            self.connection.row_factory = sqlite3.Row

    def checkDBVersion(self):

        try:
            if self.hasTable('db_version'):
                result = self.select('SELECT db_version FROM db_version')
            else:
                version = self.select('PRAGMA user_version')[0]['user_version']
                if version:
                    self.action('PRAGMA user_version = 0')
                    self.action('CREATE TABLE db_version (db_version INTEGER);')
                    self.action('INSERT INTO db_version (db_version) VALUES (%s);' % version)
                return version
        except:
            return 0

        if result:
            version = int(result[0]['db_version'])
            if 10000 > version and self.hasColumn('db_version', 'db_minor_version'):
                minor = self.select('SELECT db_minor_version FROM db_version')
                return version * 100 + int(minor[0]['db_minor_version'])
            return version
        else:
            return 0

    def mass_action(self, querylist, logTransaction=False):

        with db_lock:

            if querylist is None:
                return

            sqlResult = []
            attempt = 0

            while attempt < 5:
                try:
                    affected = 0
                    for qu in querylist:
                        cursor = self.connection.cursor()
                        if len(qu) == 1:
                            if logTransaction:
                                logger.log(qu[0], logger.DB)

                            sqlResult.append(cursor.execute(qu[0]).fetchall())
                        elif len(qu) > 1:
                            if logTransaction:
                                logger.log(qu[0] + ' with args ' + str(qu[1]), logger.DB)
                            sqlResult.append(cursor.execute(qu[0], qu[1]).fetchall())
                        affected += cursor.rowcount
                    self.connection.commit()
                    if affected > 0:
                        logger.log(u'Transaction with %s queries executed affected %i row%s' % (
                            len(querylist), affected, helpers.maybe_plural(affected)), logger.DEBUG)
                    return sqlResult
                except sqlite3.OperationalError as e:
                    sqlResult = []
                    if self.connection:
                        self.connection.rollback()
                    if 'unable to open database file' in e.args[0] or 'database is locked' in e.args[0]:
                        logger.log(u'DB error: ' + ex(e), logger.WARNING)
                        attempt += 1
                        time.sleep(1)
                    else:
                        logger.log(u'DB error: ' + ex(e), logger.ERROR)
                        raise
                except sqlite3.DatabaseError as e:
                    if self.connection:
                        self.connection.rollback()
                    logger.log(u'Fatal error executing query: ' + ex(e), logger.ERROR)
                    raise

            return sqlResult

    def action(self, query, args=None):

        with db_lock:

            if query is None:
                return

            sqlResult = None
            attempt = 0

            while attempt < 5:
                try:
                    if args is None:
                        logger.log(self.filename + ': ' + query, logger.DB)
                        sqlResult = self.connection.execute(query)
                    else:
                        logger.log(self.filename + ': ' + query + ' with args ' + str(args), logger.DB)
                        sqlResult = self.connection.execute(query, args)
                    self.connection.commit()
                    # get out of the connection attempt loop since we were successful
                    break
                except sqlite3.OperationalError as e:
                    if 'unable to open database file' in e.args[0] or 'database is locked' in e.args[0]:
                        logger.log(u'DB error: ' + ex(e), logger.WARNING)
                        attempt += 1
                        time.sleep(1)
                    else:
                        logger.log(u'DB error: ' + ex(e), logger.ERROR)
                        raise
                except sqlite3.DatabaseError as e:
                    logger.log(u'Fatal error executing query: ' + ex(e), logger.ERROR)
                    raise

            return sqlResult

    def select(self, query, args=None):

        sqlResults = self.action(query, args).fetchall()

        if sqlResults is None:
            return []

        return sqlResults

    def upsert(self, tableName, valueDict, keyDict):

        changesBefore = self.connection.total_changes

        genParams = lambda myDict: [x + ' = ?' for x in myDict.keys()]

        query = 'UPDATE [%s] SET %s WHERE %s' % (
            tableName, ', '.join(genParams(valueDict)), ' AND '.join(genParams(keyDict)))

        self.action(query, valueDict.values() + keyDict.values())

        if self.connection.total_changes == changesBefore:
            query = 'INSERT INTO [' + tableName + '] (' + ', '.join(valueDict.keys() + keyDict.keys()) + ')' + \
                    ' VALUES (' + ', '.join(['?'] * len(valueDict.keys() + keyDict.keys())) + ')'
            self.action(query, valueDict.values() + keyDict.values())

    def tableInfo(self, tableName):

        # FIXME ? binding is not supported here, but I cannot find a way to escape a string manually
        sqlResult = self.select('PRAGMA table_info([%s])' % tableName)
        columns = {}
        for column in sqlResult:
            columns[column['name']] = {'type': column['type']}
        return columns

    # http://stackoverflow.com/questions/3300464/how-can-i-get-dict-from-sqlite-query
    @staticmethod
    def _dict_factory(cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def hasTable(self, tableName):
        return len(self.select('SELECT 1 FROM sqlite_master WHERE name = ?;', (tableName, ))) > 0

    def hasColumn(self, tableName, column):
        return column in self.tableInfo(tableName)

    def hasIndex(self, tableName, index):
        sqlResults = self.select('PRAGMA index_list([%s])' % tableName)
        for result in sqlResults:
            if result['name'] == index:
                return True
        return False

    def addColumn(self, table, column, type='NUMERIC', default=0):
        self.action('ALTER TABLE [%s] ADD %s %s' % (table, column, type))
        self.action('UPDATE [%s] SET %s = ?' % (table, column), (default,))

    def has_flag(self, flag_name):
        sql_result = self.select('SELECT flag FROM flags WHERE flag = ?', [flag_name])
        if 0 < len(sql_result):
            return True
        return False

    def add_flag(self, flag_name):
        has_flag = self.has_flag(flag_name)
        if not has_flag:
            self.action('INSERT INTO flags (flag) VALUES (?)', [flag_name])
        return not has_flag

    def remove_flag(self, flag_name):
        has_flag = self.has_flag(flag_name)
        if has_flag:
            self.action('DELETE FROM flags WHERE flag = ?', [flag_name])
        return has_flag

    def toggle_flag(self, flag_name):
        """
        Add or remove a flag
        :param flag_name: Name of flag
        :type flag_name: String
        :return: True if this call added the flag, False if flag is removed
        :rtype: Boolean
        """
        if self.remove_flag(flag_name):
            return False
        self.add_flag(flag_name)
        return True

    def set_flag(self, flag_name, state=True):
        """
        Set state of flag
        :param flag_name: Name of flag
        :type flag_name: String
        :param state: If true, create flag otherwise remove flag
        :type state: Boolean
        :return: Previous state of flag
        :rtype: Boolean
        """
        return (self.add_flag, self.remove_flag)[not bool(state)](flag_name)

    def close(self):
        """Close database connection"""
        if getattr(self, 'connection', None) is not None:
            self.connection.close()
        self.connection = None

    def upgrade_log(self, to_log, log_level=logger.MESSAGE):
        logger.load_log('Upgrading %s' % self.filename, to_log, log_level)


def sanityCheckDatabase(connection, sanity_check):
    sanity_check(connection).check()


class DBSanityCheck(object):
    def __init__(self, connection):
        self.connection = connection

    def check(self):
        pass


def upgradeDatabase(connection, schema):
    logger.log(u'Checking database structure...', logger.MESSAGE)
    connection.is_upgrading = False
    _processUpgrade(connection, schema)
    if connection.is_upgrading:
        connection.upgrade_log('Finished')


def prettyName(class_name):
    return ' '.join([x.group() for x in re.finditer('([A-Z])([a-z0-9]+)', class_name)])


def restoreDatabase(filename, version):
    logger.log(u'Restoring database before trying upgrade again')
    if not sickbeard.helpers.restoreVersionedFile(dbFilename(filename=filename, suffix='v%s' % version), version):
        logger.log_error_and_exit(u'Database restore failed, abort upgrading database')
        return False
    else:
        return True


def _processUpgrade(connection, upgradeClass):
    instance = upgradeClass(connection)
    logger.log(u'Checking %s database upgrade' % prettyName(upgradeClass.__name__), logger.DEBUG)
    if not instance.test():
        connection.is_upgrading = True
        connection.upgrade_log(getattr(upgradeClass, 'pretty_name', None) or prettyName(upgradeClass.__name__))
        logger.log(u'Database upgrade required: %s' % prettyName(upgradeClass.__name__), logger.MESSAGE)
        try:
            instance.execute()
        except sqlite3.DatabaseError as e:
            # attempting to restore previous DB backup and perform upgrade
            try:
                instance.execute()
            except:
                result = connection.select('SELECT db_version FROM db_version')
                if result:
                    version = int(result[0]['db_version'])

                    # close db before attempting restore
                    connection.close()

                    if restoreDatabase(connection.filename, version):
                        logger.log_error_and_exit(u'Successfully restored database version: %s' % version)
                    else:
                        logger.log_error_and_exit(u'Failed to restore database version: %s' % version)

        logger.log('%s upgrade completed' % upgradeClass.__name__, logger.DEBUG)
    else:
        logger.log('%s upgrade not required' % upgradeClass.__name__, logger.DEBUG)

    for upgradeSubClass in upgradeClass.__subclasses__():
        _processUpgrade(connection, upgradeSubClass)


# Base migration class. All future DB changes should be subclassed from this class
class SchemaUpgrade(object):
    def __init__(self, connection, **kwargs):
        self.connection = connection

    def hasTable(self, tableName):
        return len(self.connection.select('SELECT 1 FROM sqlite_master WHERE name = ?;', (tableName, ))) > 0

    def hasColumn(self, tableName, column):
        return column in self.connection.tableInfo(tableName)

    def addColumn(self, table, column, type='NUMERIC', default=0):
        self.connection.action('ALTER TABLE [%s] ADD %s %s' % (table, column, type))
        self.connection.action('UPDATE [%s] SET %s = ?' % (table, column), (default,))

    def dropColumn(self, table, column):
        # get old table columns and store the ones we want to keep
        result = self.connection.select('pragma table_info([%s])' % table)
        keptColumns = [c for c in result if c['name'] != column]

        keptColumnsNames = []
        final = []
        pk = []

        # copy the old table schema, column by column
        for column in keptColumns:

            keptColumnsNames.append(column['name'])

            cl = [column['name'], column['type']]

            '''
            To be implemented if ever required
            if column['dflt_value']:
                cl.append(str(column['dflt_value']))

            if column['notnull']:
                cl.append(column['notnull'])
            '''

            if int(column['pk']) != 0:
                pk.append(column['name'])

            b = ' '.join(cl)
            final.append(b)

        # join all the table column creation fields
        final = ', '.join(final)
        keptColumnsNames = ', '.join(keptColumnsNames)

        # generate sql for the new table creation
        if len(pk) == 0:
            sql = 'CREATE TABLE [%s_new] (%s)' % (table, final)
        else:
            pk = ', '.join(pk)
            sql = 'CREATE TABLE [%s_new] (%s, PRIMARY KEY(%s))' % (table, final, pk)

        # create new temporary table and copy the old table data across, barring the removed column
        self.connection.action(sql)
        self.connection.action('INSERT INTO [%s_new] SELECT %s FROM [%s]' % (table, keptColumnsNames, table))

        # copy the old indexes from the old table
        result = self.connection.select("SELECT sql FROM sqlite_master WHERE tbl_name=? and type='index'", [table])

        # remove the old table and rename the new table to take it's place
        self.connection.action('DROP TABLE [%s]' % table)
        self.connection.action('ALTER TABLE [%s_new] RENAME TO [%s]' % (table, table))

        # write any indexes to the new table
        if len(result) > 0:
            for index in result:
                self.connection.action(index['sql'])

        # vacuum the db as we will have a lot of space to reclaim after dropping tables
        self.connection.action('VACUUM')

    def checkDBVersion(self):
        return self.connection.checkDBVersion()

    def incDBVersion(self):
        new_version = self.checkDBVersion() + 1
        self.connection.action('UPDATE db_version SET db_version = ?', [new_version])
        return new_version

    def setDBVersion(self, new_version):
        self.connection.action('UPDATE db_version SET db_version = ?', [new_version])
        return new_version

    def listTables(self):
        tables = []
        sql_result = self.connection.select('SELECT name FROM sqlite_master where type = "table"')
        for table in sql_result:
            tables.append(table[0])
        return tables

    def do_query(self, queries):
        if not isinstance(queries, list):
            queries = list(queries)
        elif isinstance(queries[0], list):
            queries = [item for sublist in queries for item in sublist]

        for query in queries:
            tbl_name = re.findall('(?i)DROP.*?TABLE.*?\[?([^\s\]]+)', query)
            if tbl_name and not self.hasTable(tbl_name[0]):
                continue
            tbl_name = re.findall('(?i)CREATE.*?TABLE.*?\s([^\s(]+)\s*\(', query)
            if tbl_name and self.hasTable(tbl_name[0]):
                continue
            self.connection.action(query)

    def finish(self, tbl_dropped=False):
        if tbl_dropped:
            self.connection.action('VACUUM')
        self.incDBVersion()

    def upgrade_log(self, *args, **kwargs):
        self.connection.upgrade_log(*args, **kwargs)


def MigrationCode(myDB):
    schema = {
        0: sickbeard.mainDB.InitialSchema,
        9: sickbeard.mainDB.AddSizeAndSceneNameFields,
        10: sickbeard.mainDB.RenameSeasonFolders,
        11: sickbeard.mainDB.Add1080pAndRawHDQualities,
        12: sickbeard.mainDB.AddShowidTvdbidIndex,
        13: sickbeard.mainDB.AddLastUpdateTVDB,
        14: sickbeard.mainDB.AddDBIncreaseTo15,
        15: sickbeard.mainDB.AddIMDbInfo,
        16: sickbeard.mainDB.AddProperNamingSupport,
        17: sickbeard.mainDB.AddEmailSubscriptionTable,
        18: sickbeard.mainDB.AddProperSearch,
        19: sickbeard.mainDB.AddDvdOrderOption,
        20: sickbeard.mainDB.AddSubtitlesSupport,
        21: sickbeard.mainDB.ConvertTVShowsToIndexerScheme,
        22: sickbeard.mainDB.ConvertTVEpisodesToIndexerScheme,
        23: sickbeard.mainDB.ConvertIMDBInfoToIndexerScheme,
        24: sickbeard.mainDB.ConvertInfoToIndexerScheme,
        25: sickbeard.mainDB.AddArchiveFirstMatchOption,
        26: sickbeard.mainDB.AddSceneNumbering,
        27: sickbeard.mainDB.ConvertIndexerToInteger,
        28: sickbeard.mainDB.AddRequireAndIgnoreWords,
        29: sickbeard.mainDB.AddSportsOption,
        30: sickbeard.mainDB.AddSceneNumberingToTvEpisodes,
        31: sickbeard.mainDB.AddAnimeTVShow,
        32: sickbeard.mainDB.AddAbsoluteNumbering,
        33: sickbeard.mainDB.AddSceneAbsoluteNumbering,
        34: sickbeard.mainDB.AddAnimeBlacklistWhitelist,
        35: sickbeard.mainDB.AddSceneAbsoluteNumbering2,
        36: sickbeard.mainDB.AddXemRefresh,
        37: sickbeard.mainDB.AddSceneToTvShows,
        38: sickbeard.mainDB.AddIndexerMapping,
        39: sickbeard.mainDB.AddVersionToTvEpisodes,

        40: sickbeard.mainDB.BumpDatabaseVersion,
        41: sickbeard.mainDB.Migrate41,
        42: sickbeard.mainDB.Migrate41,
        43: sickbeard.mainDB.Migrate43,
        44: sickbeard.mainDB.Migrate43,

        4301: sickbeard.mainDB.Migrate4301,
        4302: sickbeard.mainDB.Migrate4302,
        4400: sickbeard.mainDB.Migrate4302,

        5816: sickbeard.mainDB.MigrateUpstream,
        5817: sickbeard.mainDB.MigrateUpstream,
        5818: sickbeard.mainDB.MigrateUpstream,

        10000: sickbeard.mainDB.SickGearDatabaseVersion,
        10001: sickbeard.mainDB.RemoveDefaultEpStatusFromTvShows,
        10002: sickbeard.mainDB.RemoveMinorDBVersion,
        10003: sickbeard.mainDB.RemoveMetadataSub,

        20000: sickbeard.mainDB.DBIncreaseTo20001,
        20001: sickbeard.mainDB.AddTvShowOverview,
        20002: sickbeard.mainDB.AddTvShowTags,
        20003: sickbeard.mainDB.ChangeMapIndexer,
        20004: sickbeard.mainDB.AddShowNotFoundCounter,
        20005: sickbeard.mainDB.AddFlagTable,
        20006: sickbeard.mainDB.DBIncreaseTo20007,
        20007: sickbeard.mainDB.AddWebdlTypesTable,
        20008: sickbeard.mainDB.AddWatched,
        20009: sickbeard.mainDB.AddPrune,
        # 20002: sickbeard.mainDB.AddCoolSickGearFeature3,
    }

    db_version = myDB.checkDBVersion()
    logger.log(u'Detected database version: v%s' % db_version, logger.DEBUG)

    if not (db_version in schema):
        if db_version == sickbeard.mainDB.MAX_DB_VERSION:
            logger.log(u'Database schema is up-to-date, no upgrade required')
        elif db_version < 10000:
            logger.log_error_and_exit(u'SickGear does not currently support upgrading from this database version')
        else:
            logger.log_error_and_exit(u'Invalid database version')

    else:

        myDB.upgrade_log('Upgrading')
        while db_version < sickbeard.mainDB.MAX_DB_VERSION:
            if None is schema[db_version]:  # skip placeholders used when multi PRs are updating DB
                db_version += 1
                continue
            try:
                update = schema[db_version](myDB)
                db_version = update.execute()
            except Exception as e:
                myDB.close()
                logger.log(u'Failed to update database with error: %s attempting recovery...' % ex(e), logger.ERROR)

                if restoreDatabase(myDB.filename, db_version):
                    # initialize the main SB database
                    logger.log_error_and_exit(u'Successfully restored database version: %s' % db_version)
                else:
                    logger.log_error_and_exit(u'Failed to restore database version: %s' % db_version)
        myDB.upgrade_log('Finished')


def backup_database(filename, version):
    logger.log(u'Backing up database before upgrade')
    if not sickbeard.helpers.backupVersionedFile(dbFilename(filename), version):
        logger.log_error_and_exit(u'Database backup failed, abort upgrading database')
    else:
        logger.log(u'Proceeding with upgrade')


def get_rollback_module():
    import imp

    module_urls = [
        'https://raw.githubusercontent.com/SickGear/sickgear.extdata/master/SickGear/Rollback/rollback.py']

    try:
        hdr = '# SickGear Rollback Module'
        module = ''
        fetched = False

        for t in range(1, 4):
            for url in module_urls:
                try:
                    module = helpers.getURL(url)
                    if module and module.startswith(hdr):
                        fetched = True
                        break
                except (StandardError, Exception):
                    continue
            if fetched:
                break
            time.sleep(30)

        if fetched:
            loaded = imp.new_module('DbRollback')
            exec(module, loaded.__dict__)
            return loaded

    except (StandardError, Exception):
        pass

    return None
