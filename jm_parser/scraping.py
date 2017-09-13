import os
from contextlib import ExitStack

import click
import requests
import scrapy
from scrapy.crawler import Crawler, CrawlerRunner
from scrapy.settings import Settings
from twisted.internet import reactor
from twisted.internet.error import ReactorNotRestartable

SCRAPY_SETTINGS = Settings({
    'LOG_LEVEL': 'WARNING'
})


def download_file(url, local_filename, allow_prompt=False):
    if os.path.exists(local_filename):
        # don't redownload if file exists
        if allow_prompt:
            click.echo('{} exists, skipping download.'.format(local_filename))
        return
    # stream for chunked download processing
    r = requests.get(url, stream=True)
    with open(local_filename, 'wb') as f:
        # instantiate a bar context manager:
        # - if we're allowed to prompt the user, and know how large the file we're downloading is,
        #   display a "real" progress bar
        # - otherwise, use ExitStack, which is a noop context manager
        if allow_prompt and r.headers.get('content-length'):
            bar = click.progressbar(length=int(r.headers['content-length']),
                                    label='Downloading {}'.format(local_filename))
        else:
            bar = ExitStack()

        with bar as bar_ctx:
            # chunk size results in decent download speeds without hitting memory too hard, while
            # also giving us decently frequent progress updates in the likely event we ended up
            # displaying a progress bar
            for chunk in r.iter_content(chunk_size=4096):
                f.write(chunk)
                if not isinstance(bar_ctx, ExitStack):
                    bar_ctx.update(len(chunk))


class JenkinsRPMScraper(scrapy.Spider):
    name = 'jenkins_rpm_scraper'
    start_urls = [
        'https://pkg.jenkins.io/redhat-stable/'
    ]
    href_template = 'https://pkg.jenkins.io/redhat-stable/{}'

    def parse(self, response):
        match_str = './jenkins-{}.'.format(self.xy_version)
        anchors = response.selector.xpath('//a[starts-with(@href, "{}")]'.format(match_str))
        for a in anchors:
            # anchor text is in the format 'jenkins-x.y.z-relx.rely.rpm', which is what we want
            href = self.href_template.format(a.root.text)
            filename = os.path.join(self.dist_dir, 'rpm', a.root.text)
            download_file(href, filename, self.allow_prompt)


class JenkinsWarScraper(scrapy.Spider):
    name = 'jenkins_war_scraper'
    start_urls = [
        'http://mirrors.jenkins.io/war-stable/'
    ]
    href_template = 'http://mirrors.jenkins.io/war-stable/{}/jenkins.war'

    def parse(self, response):
        match_str = '{}.'.format(self.xy_version)
        anchors = response.selector.xpath('//a[starts-with(@href, "{}")]'.format(match_str))
        for a in anchors:
            # anchor text is in the format "x.y.z/", so strip slashes to generate the URL,
            # and generate a new filename based on the xyz version
            xyz_version = a.root.text.strip('/')
            href = self.href_template.format(xyz_version)
            filename = os.path.join(self.dist_dir, 'war', 'jenkins-{}.war'.format(xyz_version))
            download_file(href, filename, self.allow_prompt)


def scrape_for_versions(xy_versions, dist_dir, allow_prompt=False):
    spider_kwargs = {
        'dist_dir': dist_dir,
        'allow_prompt': allow_prompt,
    }

    runner = CrawlerRunner()
    for spider_class in (JenkinsRPMScraper, JenkinsWarScraper):
        for xy_version in xy_versions:
            crawler = Crawler(spider_class, SCRAPY_SETTINGS)
            runner.crawl(crawler, xy_version=xy_version, **spider_kwargs)
    deferred = runner.join()
    # stop the reactor on success or error
    deferred.addBoth(lambda _: reactor.stop())
    try:
        reactor.run()
    except ReactorNotRestartable:
        # This is an expection. We aren't trying to restart the reactor at this point,
        # since it should have been stopped with the callback. Regardless, twisted still
        # throws this exception and I didn't feel terribly interested in finding out why.
        pass
