# coding=utf-8
# Author: Mr_Orange <mr_orange@hotmail.it>
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

import sys
import os
import traceback
import urllib
import re
import datetime
import urlparse

import sickbeard
import generic
from sickbeard.common import Quality
from sickbeard.name_parser.parser import NameParser, InvalidNameException, InvalidShowException
from sickbeard import logger
from sickbeard import tvcache
from sickbeard import helpers
from sickbeard import db
from sickbeard import classes
from sickbeard.show_name_helpers import allPossibleShowNames, sanitizeSceneName
from sickbeard.exceptions import ex
from sickbeard import encodingKludge as ek
from sickbeard import clients
from sickbeard.bs4_parser import BS4Parser
from lib import requests
from lib.requests import exceptions
from lib.unidecode import unidecode


class KATProvider(generic.TorrentProvider):
    def __init__(self):

        generic.TorrentProvider.__init__(self, "KickAssTorrents")

        self.supportsBacklog = True

        self.enabled = False
        self.confirmed = False
        self.ratio = None
        self.minseed = None
        self.minleech = None

        self.cache = KATCache(self)

        self.urls = ['http://kickass.to/', 'http://katproxy.com/', 'http://www.kickmirror.com/']
        self.url = 'https://kickass.to/'

    def isEnabled(self):
        return self.enabled

    def imageName(self):
        return 'kat.png'

    def getQuality(self, item, anime=False):

        quality = Quality.sceneQuality(item[0], anime)
        return quality

    def _reverseQuality(self, quality):

        quality_string = ''

        if quality == Quality.SDTV:
            quality_string = 'HDTV x264'
        if quality == Quality.SDDVD:
            quality_string = 'DVDRIP'
        elif quality == Quality.HDTV:
            quality_string = '720p HDTV x264'
        elif quality == Quality.FULLHDTV:
            quality_string = '1080p HDTV x264'
        elif quality == Quality.RAWHDTV:
            quality_string = '1080i HDTV mpeg2'
        elif quality == Quality.HDWEBDL:
            quality_string = '720p WEB-DL h264'
        elif quality == Quality.FULLHDWEBDL:
            quality_string = '1080p WEB-DL h264'
        elif quality == Quality.HDBLURAY:
            quality_string = '720p Bluray x264'
        elif quality == Quality.FULLHDBLURAY:
            quality_string = '1080p Bluray x264'

        return quality_string

    def _find_season_quality(self, title, torrent_link, ep_number):
        """ Return the modified title of a Season Torrent with the quality found inspecting torrent file list """

        mediaExtensions = ['avi', 'mkv', 'wmv', 'divx',
                           'vob', 'dvr-ms', 'wtv', 'ts'
                                                   'ogv', 'rar', 'zip', 'mp4']

        quality = Quality.UNKNOWN

        fileName = None

        data = self.getURL(torrent_link)
        if not data:
            return None

        try:
            with BS4Parser(data, features=["html5lib", "permissive"]) as soup:
                file_table = soup.find('table', attrs={'class': 'torrentFileList'})

                if not file_table:
                    return None

                files = [x.text for x in file_table.find_all('td', attrs={'class': 'torFileName'})]
                videoFiles = filter(lambda x: x.rpartition(".")[2].lower() in mediaExtensions, files)

                #Filtering SingleEpisode/MultiSeason Torrent
                if len(videoFiles) < ep_number or len(videoFiles) > float(ep_number * 1.1):
                    logger.log(u"Result " + title + " have " + str(
                        ep_number) + " episode and episodes retrived in torrent are " + str(len(videoFiles)), logger.DEBUG)
                    logger.log(
                        u"Result " + title + " Seem to be a Single Episode or MultiSeason torrent, skipping result...",
                        logger.DEBUG)
                    return None

                if Quality.sceneQuality(title) != Quality.UNKNOWN:
                    return title

                for fileName in videoFiles:
                    quality = Quality.sceneQuality(os.path.basename(fileName))
                    if quality != Quality.UNKNOWN: break

                if fileName is not None and quality == Quality.UNKNOWN:
                    quality = Quality.assumeQuality(os.path.basename(fileName))

                if quality == Quality.UNKNOWN:
                    logger.log(u"Unable to obtain a Season Quality for " + title, logger.DEBUG)
                    return None

                try:
                    myParser = NameParser(showObj=self.show)
                    parse_result = myParser.parse(fileName)
                except (InvalidNameException, InvalidShowException):
                    return None

                logger.log(u"Season quality for " + title + " is " + Quality.qualityStrings[quality], logger.DEBUG)

                if parse_result.series_name and parse_result.season_number:
                    title = parse_result.series_name + ' S%02d' % int(
                        parse_result.season_number) + ' ' + self._reverseQuality(quality)

                return title

        except Exception, e:
            logger.log(u"Failed parsing " + self.name + " Traceback: " + traceback.format_exc(), logger.ERROR)


    def _get_season_search_strings(self, ep_obj):
        search_string = {'Season': []}

        for show_name in set(allPossibleShowNames(self.show)):
            if ep_obj.show.air_by_date or ep_obj.show.sports:
                ep_string = show_name + ' ' + str(ep_obj.airdate).split('-')[0]
                search_string['Season'].append(ep_string)
                ep_string = show_name + ' Season ' + str(ep_obj.airdate).split('-')[0]
                search_string['Season'].append(ep_string)
            elif ep_obj.show.anime:
                ep_string = show_name + ' ' + "%02d" % ep_obj.scene_absolute_number
                search_string['Season'].append(ep_string)
            else:
                ep_string = show_name + ' S%02d' % int(ep_obj.scene_season) + ' -S%02d' % int(
                    ep_obj.scene_season) + 'E' + ' category:tv'  #1) showName SXX -SXXE
                search_string['Season'].append(ep_string)
                ep_string = show_name + ' Season ' + str(
                    ep_obj.scene_season) + ' -Ep*' + ' category:tv'  # 2) showName Season X
                search_string['Season'].append(ep_string)

        return [search_string]

    def _get_episode_search_strings(self, ep_obj, add_string=''):
        search_string = {'Episode': []}

        if self.show.air_by_date:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            str(ep_obj.airdate).replace('-', ' ')
                search_string['Episode'].append(ep_string)
        elif self.show.sports:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            str(ep_obj.airdate).replace('-', '|') + '|' + \
                            ep_obj.airdate.strftime('%b')
                search_string['Episode'].append(ep_string)
        elif self.show.anime:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            "%02i" % int(ep_obj.scene_absolute_number)
                search_string['Episode'].append(ep_string)
        else:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            sickbeard.config.naming_ep_type[2] % {'seasonnumber': ep_obj.scene_season,
                                                                  'episodenumber': ep_obj.scene_episode} + '|' + \
                            sickbeard.config.naming_ep_type[0] % {'seasonnumber': ep_obj.scene_season,
                                                                  'episodenumber': ep_obj.scene_episode} + ' %s category:tv' % add_string
                search_string['Episode'].append(re.sub('\s+', ' ', ep_string))

        return [search_string]


    def _doSearch(self, search_params, search_mode='eponly', epcount=0, age=0):

        results = []
        items = {'Season': [], 'Episode': [], 'RSS': []}

        for mode in search_params.keys():
            for search_string in search_params[mode]:

                for url in self.urls:
                    if mode != 'RSS':
                        searchURL = url + 'usearch/%s/?field=seeders&sorder=desc' % (urllib.quote(unidecode(search_string)))
                        logger.log(u"Search string: " + searchURL, logger.DEBUG)
                    else:
                        searchURL = url + 'tv/?field=time_add&sorder=desc'
                        logger.log(u"KAT cache update URL: " + searchURL, logger.DEBUG)

                    html = self.getURL(searchURL)
                    if html:
                        self.url = url
                        break

                if not html:
                    continue

                try:
                    with BS4Parser(html, features=["html5lib", "permissive"]) as soup:
                        torrent_table = soup.find('table', attrs={'class': 'data'})
                        torrent_rows = torrent_table.find_all('tr') if torrent_table else []

                        #Continue only if one Release is found
                        if len(torrent_rows) < 2:
                            logger.log(u"The data returned from " + self.name + " does not contain any torrents",
                                       logger.WARNING)
                            continue

                        for tr in torrent_rows[1:]:
                            try:
                                link = urlparse.urljoin(self.url,
                                                        (tr.find('div', {'class': 'torrentname'}).find_all('a')[1])['href'])
                                id = tr.get('id')[-7:]
                                title = (tr.find('div', {'class': 'torrentname'}).find_all('a')[1]).text \
                                        or (tr.find('div', {'class': 'torrentname'}).find_all('a')[2]).text
                                url = tr.find('a', 'imagnet')['href']
                                verified = True if tr.find('a', 'iverify') else False
                                trusted = True if tr.find('img', {'alt': 'verified'}) else False
                                seeders = int(tr.find_all('td')[-2].text)
                                leechers = int(tr.find_all('td')[-1].text)
                            except (AttributeError, TypeError):
                                continue

                            if mode != 'RSS' and (seeders < self.minseed or leechers < self.minleech):
                                continue

                            if self.confirmed and not verified:
                                logger.log(
                                    u"KAT Provider found result " + title + " but that doesn't seem like a verified result so I'm ignoring it",
                                    logger.DEBUG)
                                continue

                            #Check number video files = episode in season and find the real Quality for full season torrent analyzing files in torrent
                            if mode == 'Season' and search_mode == 'sponly':
                                ep_number = int(epcount / len(set(allPossibleShowNames(self.show))))
                                title = self._find_season_quality(title, link, ep_number)

                            if not title or not url:
                                continue

                            item = title, url, id, seeders, leechers

                            items[mode].append(item)

                except Exception, e:
                    logger.log(u"Failed to parsing " + self.name + " Traceback: " + traceback.format_exc(),
                               logger.ERROR)

            #For each search mode sort all the items by seeders
            items[mode].sort(key=lambda tup: tup[3], reverse=True)

            results += items[mode]

        return results

    def _get_title_and_url(self, item):

        title, url, id, seeders, leechers = item

        if title:
            title = u'' + title
            title = title.replace(' ', '.')

        if url:
            url = url.replace('&amp;', '&')

        return (title, url)

    def findPropers(self, search_date=datetime.datetime.today()):

        results = []

        myDB = db.DBConnection()
        sqlResults = myDB.select(
            'SELECT s.show_name, e.showid, e.season, e.episode, e.status, e.airdate, s.indexer FROM tv_episodes AS e' +
            ' INNER JOIN tv_shows AS s ON (e.showid = s.indexer_id)' +
            ' WHERE e.airdate >= ' + str(search_date.toordinal()) +
            ' AND (e.status IN (' + ','.join([str(x) for x in Quality.DOWNLOADED]) + ')' +
            ' OR (e.status IN (' + ','.join([str(x) for x in Quality.SNATCHED]) + ')))'
        )

        if not sqlResults:
            return []

        for sqlshow in sqlResults:
            self.show = helpers.findCertainShow(sickbeard.showList, int(sqlshow["showid"]))
            if self.show:
                curEp = self.show.getEpisode(int(sqlshow["season"]), int(sqlshow["episode"]))

                searchString = self._get_episode_search_strings(curEp, add_string='PROPER|REPACK')

                for item in self._doSearch(searchString[0]):
                    title, url = self._get_title_and_url(item)
                    results.append(classes.Proper(title, url, datetime.datetime.today(), self.show))

        return results

    def seedRatio(self):
        return self.ratio


class KATCache(tvcache.TVCache):
    def __init__(self, provider):

        tvcache.TVCache.__init__(self, provider)

        # only poll ThePirateBay every 10 minutes max
        self.minTime = 20

    def _getRSSData(self):
        search_params = {'RSS': ['rss']}
        return self.provider._doSearch(search_params)


provider = KATProvider()
