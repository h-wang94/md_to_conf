#!/usr/bin/python3
"""
# --------------------------------------------------------------------------------------------------
# Rittman Mead Markdown to Confluence Tool
# --------------------------------------------------------------------------------------------------
# Create or Update Atlas pages remotely using markdown files.
#
# --------------------------------------------------------------------------------------------------
# Usage: rest_md2conf.py markdown spacekey
# --------------------------------------------------------------------------------------------------
"""

import logging
import sys
import os
import re
import json
import collections
import mimetypes
import codecs
import argparse
import urllib
import webbrowser
import requests
import markdown
import convert

from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - \
%(levelname)s - %(funcName)s [%(lineno)d] - \
%(message)s')
LOGGER = logging.getLogger(__name__)

# ArgumentParser to parse arguments and options
PARSER = argparse.ArgumentParser()
PARSER.add_argument("markdownFile", help="Full path of the markdown file to convert and upload.")
PARSER.add_argument('spacekey',
                    help="Confluence Space key for the page. If omitted, will use user space.")
PARSER.add_argument('-u', '--username', help='Confluence username if $CONFLUENCE_USERNAME not set.')
PARSER.add_argument('-p', '--apikey', help='Confluence API key if $CONFLUENCE_API_KEY not set.')
PARSER.add_argument('-o', '--orgname',
                    help='Confluence organisation if $CONFLUENCE_ORGNAME not set. '
                         'e.g. https://XXX.atlassian.net/wiki'
                         'If orgname contains a dot, considered as the fully qualified domain name.'
                         'e.g. https://XXX')
PARSER.add_argument('-a', '--ancestor',
                    help='Parent page under which page will be created or moved.')
PARSER.add_argument('-t', '--attachment', nargs='+',
                    help='Attachment(s) to upload to page. Paths relative to the markdown file.')
PARSER.add_argument('-c', '--contents', action='store_true', default=False,
                    help='Use this option to generate a contents page.')
PARSER.add_argument('-g', '--nogo', action='store_true', default=False,
                    help='Use this option to skip navigation after upload.')
PARSER.add_argument('-n', '--nossl', action='store_true', default=False,
                    help='Use this option if NOT using SSL. Will use HTTP instead of HTTPS.')
PARSER.add_argument('-d', '--delete', action='store_true', default=False,
                    help='Use this option to delete the page instead of create it.')
PARSER.add_argument('-l', '--loglevel', default='INFO',
                    help='Use this option to set the log verbosity.')
PARSER.add_argument('-s', '--simulate', action='store_true', default=False,
                    help='Use this option to only show conversion result.')
PARSER.add_argument('-v', '--version', type=int, action='store', default=1,
                    help='Version of confluence page (default is 1).')
PARSER.add_argument('-mds', '--markdownsrc', action='store', default='',
                    help='Use this option to specify a markdown source (i.e. what processor this markdown was targeting). '
                         'Possible values: bitbucket.')
PARSER.add_argument('--label', action='append', dest='labels', default=[],
                    help='A list of labels to set on the page.')
PARSER.add_argument('--property', action='append', dest='properties', default=[],
                    type=lambda kv: kv.split("="),
                    help='A list of content properties to set on the page.')
PARSER.add_argument('-T', '--tag', action='store', dest='sha_tag', help='Git code tag or SHA1')
PARSER.add_argument('-S', '--scmprefix', action='store', dest='scm_prefix', help='SCM prefix (appended with tag and the path to the markdownFile)')

ARGS = PARSER.parse_args()

# Assign global variables
try:
    # Set log level
    LOGGER.setLevel(getattr(logging, ARGS.loglevel.upper(), None))

    MARKDOWN_FILE = ARGS.markdownFile
    SPACE_KEY = ARGS.spacekey
    USERNAME = os.getenv('CONFLUENCE_USERNAME', ARGS.username)
    API_KEY = os.getenv('CONFLUENCE_API_KEY', ARGS.apikey)
    ORGNAME = os.getenv('CONFLUENCE_ORGNAME', ARGS.orgname)
    ANCESTOR = ARGS.ancestor
    NOSSL = ARGS.nossl
    DELETE = ARGS.delete
    SIMULATE = ARGS.simulate
    VERSION = ARGS.version
    MARKDOWN_SOURCE = ARGS.markdownsrc
    LABELS = ARGS.labels
    PROPERTIES = dict(ARGS.properties)
    ATTACHMENTS = ARGS.attachment
    GO_TO_PAGE = not ARGS.nogo
    CONTENTS = ARGS.contents
    SHA_TAG = ARGS.sha_tag
    SCM_PREFIX = ARGS.scm_prefix

    if USERNAME is None:
        LOGGER.error('Error: Username not specified by environment variable or option.')
        sys.exit(1)

    if API_KEY is None:
        LOGGER.error('Error: API key not specified by environment variable or option.')
        sys.exit(1)

    if not os.path.exists(MARKDOWN_FILE):
        LOGGER.error('Error: Markdown file: %s does not exist.', MARKDOWN_FILE)
        sys.exit(1)

    if SPACE_KEY is None:
        SPACE_KEY = '~%s' % (USERNAME)

    if ORGNAME is not None:
        if ORGNAME.find('.') != -1:
            CONFLUENCE_API_URL = 'https://%s' % ORGNAME
        else:
            CONFLUENCE_API_URL = 'https://%s.atlassian.net/wiki' % ORGNAME
    else:
        LOGGER.error('Error: Org Name not specified by environment variable or option.')
        sys.exit(1)

    if NOSSL:
        CONFLUENCE_API_URL.replace('https://', 'http://')

except Exception as err:
    LOGGER.error('\n\nException caught:\n%s ', err)
    LOGGER.error('\nFailed to process command line arguments. Exiting.')
    sys.exit(1)

def get_page(title):
    """
     Retrieve page details by title

    :param title: page tile
    :return: Confluence page info
    """
    LOGGER.info('\tRetrieving page information: %s', title)
    url = '%s/rest/api/content?title=%s&spaceKey=%s&expand=version,ancestors' % (
        CONFLUENCE_API_URL, urllib.parse.quote_plus(title), SPACE_KEY)

    # We retrieve content property values as part of page content
    # to make sure we are able to update them later
    if PROPERTIES:
        url = '%s,%s' % (url, ','.join("metadata.properties.%s" % v for v in PROPERTIES.keys()))

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)

    response = session.get(url)

    # Check for errors
    try:
        response.raise_for_status()
    except requests.RequestException as err:
        LOGGER.error('err.response: %s', err)
        if response.status_code == 404:
            LOGGER.error('Error: Page not found. Check the following are correct:')
            LOGGER.error('\tSpace Key : %s', SPACE_KEY)
            LOGGER.error('\tOrganisation Name: %s', ORGNAME)
        else:
            LOGGER.error('Error: %d - %s', response.status_code, response.content)
        sys.exit(1)

    data = response.json()

    LOGGER.debug("data: %s", str(data))

    if len(data[u'results']) >= 1:
        page_id = data[u'results'][0][u'id']
        version_num = data[u'results'][0][u'version'][u'number']
        link = '%s%s' % (CONFLUENCE_API_URL, data[u'results'][0][u'_links'][u'webui'])

        try:
            LOGGER.info(str(data))
            properties = data[u'results'][0][u'metadata'][u'properties']

        except KeyError:
            # In case when page has no content properties we can simply ignore them
            properties = {}
            pass

        page_info = collections.namedtuple('PageInfo', ['id', 'version', 'link', 'properties'])
        page = page_info(page_id, version_num, link, properties)
        return page

    return False


# Scan for images and upload as attachments if found
def add_images(page_id, html):
    """
    Scan for images and upload as attachments if found

    :param page_id: Confluence page id
    :param html: html string
    :return: html with modified image reference
    """
    source_folder = os.path.dirname(os.path.abspath(MARKDOWN_FILE))

    for tag in re.findall('<img(.*?)\/>', html):
        rel_path = re.search('src="(.*?)"', tag).group(1)
        alt_text = re.search('alt="(.*?)"', tag).group(1)
        abs_path = os.path.join(source_folder, rel_path)
        basename = os.path.basename(rel_path)
        upload_attachment(page_id, abs_path, alt_text)
        if re.search('http.*', rel_path) is None:
            if CONFLUENCE_API_URL.endswith('/wiki'):
                html = html.replace('%s' % (rel_path),
                                    '/wiki/download/attachments/%s/%s' % (page_id, basename))
            else:
                html = html.replace('%s' % (rel_path),
                                    '/download/attachments/%s/%s' % (page_id, basename))
    return html



def add_attachments(page_id, files):
    """
    Add attachments for an array of files

    :param page_id: Confluence page id
    :param files: list of files to attach to the given Confluence page
    :return: None
    """
    source_folder = os.path.dirname(os.path.abspath(MARKDOWN_FILE))

    if files:
        for file in files:
            upload_attachment(page_id, os.path.join(source_folder, file), '')


def add_local_refs(page_id, title, html):
    """
    Convert local links to correct confluence local links

    :param page_title: string
    :param page_id: integer
    :param html: string
    :return: modified html string
    """

    ref_prefixes = {
      "bitbucket": "#markdown-header-"
    }
    ref_postfixes = {
      "bitbucket": "_%d"
    }

    # We ignore local references in case of unknown or unspecified markdown source
    if not MARKDOWN_SOURCE in ref_prefixes or \
       not MARKDOWN_SOURCE in ref_postfixes:
        LOGGER.warning('Local references weren''t processed because '
                       '--markdownsrc wasn''t set or specified source isn''t supported')
        return html

    ref_prefix = ref_prefixes[MARKDOWN_SOURCE]
    ref_postfix = ref_postfixes[MARKDOWN_SOURCE]

    LOGGER.info('Converting confluence local links...')

    headers = re.findall(r'<h\d+>(.*?)</h\d+>', html, re.DOTALL)
    if headers:
        headers_map = {}
        headers_count = {}

        for header in headers:
            key = ref_prefix + slug(header, True)

            if VERSION == 1:
                value = ''.join(header.split())
            if VERSION == 2:
                value = slug(header, False)

            if key in headers_map:
                alt_count = headers_count[key]

                alt_key = key + (ref_postfix % alt_count)
                alt_value = value + ('.%s' % alt_count)

                headers_map[alt_key] = alt_value
                headers_count[key] = alt_count + 1
            else:
                headers_map[key] = value
                headers_count[key] = 1

        links = re.findall(r'<a href="#.+?">.+?</a>', html)
        if links:
            for link in links:
                matches = re.search(r'<a href="(#.+?)">(.+?)</a>', link)
                ref = matches.group(1)
                alt = matches.group(2)

                result_ref = headers_map[ref]

                if result_ref:
                    base_uri = '%s/spaces/%s/pages/%s/%s' % (CONFLUENCE_API_URL, SPACE_KEY, page_id, '+'.join(title.split()))
                    if VERSION == 1:
                        replacement_uri = '%s#%s-%s' % (base_uri, ''.join(title.split()), result_ref)
                    if VERSION == 2:
                        replacement_uri = '%s#%s' % (base_uri, result_ref)

                    replacement = '<a href="%s" title="%s">%s</a>' % (replacement_uri, alt, alt)
                    html = html.replace(link, replacement)

    return html


def create_page(title, body, ancestors):
    """
    Create a new page

    :param title: confluence page title
    :param body: confluence page content
    :param ancestors: confluence page ancestor
    :return:
    """
    LOGGER.info('Creating page...')

    url = '%s/rest/api/content/' % CONFLUENCE_API_URL

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)
    session.headers.update({'Content-Type': 'application/json'})

    new_page = {'type': 'page', \
               'title': title, \
               'space': {'key': SPACE_KEY}, \
               'body': { \
                   'storage': { \
                       'value': body, \
                       'representation': 'storage' \
                       } \
                   }, \
               'ancestors': ancestors, \
               'metadata': { \
                   'properties': { \
            	  	     'editor': { \
            	  		       'value': 'v%d' % VERSION \
            	  	         } \
              	       } \
                   } \
               }

    LOGGER.debug("data: %s", json.dumps(new_page))

    response = session.post(url, data=json.dumps(new_page))
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as excpt:
        LOGGER.error("error: %s - %s", excpt, response.content)
        exit(1)

    if response.status_code == 200:
        data = response.json()
        space_name = data[u'space'][u'name']
        page_id = data[u'id']
        version = data[u'version'][u'number']
        link = '%s%s' % (CONFLUENCE_API_URL, data[u'_links'][u'webui'])

        LOGGER.info('Page created in %s with ID: %s.', space_name, page_id)
        LOGGER.info('URL: %s', link)

        # Populate properties dictionary with initial property values
        properties = {}
        if PROPERTIES:
            for key in PROPERTIES:
                properties[key] = {"key": key, "version": 1, "value": PROPERTIES[key]}

        img_check = re.search('<img(.*?)\/>', body)
        local_ref_check = re.search('<a href="(#.+?)">(.+?)</a>', body)
        if img_check or local_ref_check or properties or ATTACHMENTS or LABELS:
            LOGGER.info('\tAttachments, local references, content properties or labels found, update procedure called.')
            update_page(page_id, title, body, version, ancestors, properties, ATTACHMENTS)
        else:
            if GO_TO_PAGE:
                webbrowser.open(link)
    else:
        LOGGER.error('Could not create page.')
        sys.exit(1)


def delete_page(page_id):
    """
    Delete a page

    :param page_id: confluence page id
    :return: None
    """
    LOGGER.info('Deleting page...')
    url = '%s/rest/api/content/%s' % (CONFLUENCE_API_URL, page_id)

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)
    session.headers.update({'Content-Type': 'application/json'})

    response = session.delete(url)
    response.raise_for_status()

    if response.status_code == 204:
        LOGGER.info('Page %s deleted successfully.', page_id)
    else:
        LOGGER.error('Page %s could not be deleted.', page_id)


def update_page(page_id, title, body, version, ancestors, properties, attachments):
    """
    Update a page

    :param page_id: confluence page id
    :param title: confluence page title
    :param body: confluence page content
    :param version: confluence page version
    :param ancestors: confluence page ancestor
    :param attachments: confluence page attachments
    :return: None
    """
    LOGGER.info('Updating page...')

    # Add images and attachments
    body = add_images(page_id, body)
    add_attachments(page_id, attachments)

    # Add local references
    body = add_local_refs(page_id, title, body)

    url = '%s/rest/api/content/%s' % (CONFLUENCE_API_URL, page_id)

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)
    session.headers.update({'Content-Type': 'application/json'})

    page_json = { \
        "id": page_id, \
        "type": "page", \
        "title": title, \
        "space": {"key": SPACE_KEY}, \
        "body": { \
            "storage": { \
                "value": body, \
                "representation": "storage" \
                } \
            }, \
        "version": { \
            "number": version + 1, \
            "minorEdit" : True \
            }, \
        'ancestors': ancestors \
        }

    if LABELS:
        if 'metadata' not in page_json:
            page_json['metadata'] = {}

        labels = []
        for value in LABELS:
            labels.append({"name": value})

        page_json['metadata']['labels'] = labels

    response = session.put(url, data=json.dumps(page_json))
    response.raise_for_status()

    if response.status_code == 200:
        data = response.json()
        link = '%s%s' % (CONFLUENCE_API_URL, data[u'_links'][u'webui'])

        LOGGER.info("Page updated successfully.")
        LOGGER.info('URL: %s', link)

        if properties:
            LOGGER.info("Updating page content properties...")

            for key in properties:
                prop_url = '%s/property/%s' % (url, key)
                prop_json = {"key": key, "version": {"number": properties[key][u"version"]}, "value": properties[key][u"value"]}

                response = session.put(prop_url, data=json.dumps(prop_json))
                response.raise_for_status()

                if response.status_code == 200:
                    LOGGER.info("\tUpdated property %s", key)

        if GO_TO_PAGE:
            webbrowser.open(link)
    else:
        LOGGER.error("Page could not be updated.")


def get_attachment(page_id, filename):
    """
    Get page attachment

    :param page_id: confluence page id
    :param filename: attachment filename
    :return: attachment info in case of success, False otherwise
    """
    url = '%s/rest/api/content/%s/child/attachment?filename=%s' % (CONFLUENCE_API_URL, page_id, filename)

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)

    response = session.get(url)
    response.raise_for_status()
    data = response.json()

    if len(data[u'results']) >= 1:
        att_id = data[u'results'][0]['id']
        att_info = collections.namedtuple('AttachmentInfo', ['id'])
        attr_info = att_info(att_id)
        return attr_info

    return False


def upload_attachment(page_id, file, comment):
    """
    Upload an attachement

    :param page_id: confluence page id
    :param file: attachment file
    :param comment: attachment comment
    :return: boolean
    """
    if re.search('http.*', file):
        return False

    content_type = mimetypes.guess_type(file)[0]
    filename = os.path.basename(file)

    if not os.path.isfile(file):
        LOGGER.error('File %s cannot be found --> skip ', file)
        return False

    file_to_upload = {
        'comment': comment,
        'file': (filename, open(file, 'rb'), content_type, {'Expires': '0'})
    }

    attachment = get_attachment(page_id, filename)
    if attachment:
        url = '%s/rest/api/content/%s/child/attachment/%s/data' % (CONFLUENCE_API_URL, page_id, attachment.id)
    else:
        url = '%s/rest/api/content/%s/child/attachment/' % (CONFLUENCE_API_URL, page_id)

    session = requests.Session()
    session.auth = (USERNAME, API_KEY)
    session.headers.update({'X-Atlassian-Token': 'no-check'})

    LOGGER.info('\tUploading attachment %s...', filename)

    response = session.post(url, files=file_to_upload)
    response.raise_for_status()

    return True

def add_header(html):
    if (SHA_TAG and SCM_PREFIX):
        timestamp = datetime.strftime(datetime.now(), '%c')
        base = os.path.basename(MARKDOWN_FILE)
        bloburl = "%s/%s" % (SCM_PREFIX, os.path.join(SHA_TAG, MARKDOWN_FILE))
        blurb = "<p><i>This page was auto-generated from <a href=\"%s\">%s version %s</a> on %s</i></p>" % (bloburl, base, SHA_TAG, timestamp)
        return blurb + html
    else:
        return html


def main():
    """
    Main program

    :return:
    """
    LOGGER.info('\t\t----------------------------------')
    LOGGER.info('\t\tMarkdown to Confluence Upload Tool')
    LOGGER.info('\t\t----------------------------------\n\n')

    LOGGER.info('Markdown file:\t%s', MARKDOWN_FILE)
    LOGGER.info('Space Key:\t%s', SPACE_KEY)

    with open(MARKDOWN_FILE, 'r') as mdfile:
        title = mdfile.readline().lstrip('#').strip()
        mdfile.seek(0)

    LOGGER.info('Title:\t\t%s', title)

    with codecs.open(MARKDOWN_FILE, 'r', 'utf-8') as mdfile:
        html = markdown.markdown(mdfile.read(), extensions=['markdown.extensions.tables',
                                                       'markdown.extensions.fenced_code'])

    html = '\n'.join(html.split('\n')[1:])

    html = convert.convert_info_macros(html)
    html = convert.convert_comment_block(html)
    html = convert.convert_code_block(html)

    if CONTENTS:
        html = convert.add_contents(html)

    html = convert.process_refs(html)

    html = add_header(html)

    LOGGER.debug('html: %s', html)

    if SIMULATE:
        LOGGER.info("Simulate mode is active - stop processing here.")
        sys.exit(0)

    LOGGER.info('Checking if Atlas page exists...')
    page = get_page(title)

    if DELETE and page:
        delete_page(page.id)
        sys.exit(1)

    if ANCESTOR:
        parent_page = get_page(ANCESTOR)
        if parent_page:
            ancestors = [{'type': 'page', 'id': parent_page.id}]
        else:
            LOGGER.error('Error: Parent page does not exist: %s', ANCESTOR)
            sys.exit(1)
    else:
        ancestors = []

    if page:
        # Populate properties dictionary with updated property values
        properties = {}
        if PROPERTIES:
            for key in PROPERTIES:
                if key in page.properties:
                    properties[key] = {"key": key, "version": page.properties[key][u'version'][u'number'] + 1, "value": PROPERTIES[key]}
                else:
                    properties[key] = {"key": key, "version": 1, "value": PROPERTIES[key]}

        update_page(page.id, title, html, page.version, ancestors, properties, ATTACHMENTS)
    else:
        create_page(title, html, ancestors)

    LOGGER.info('Markdown Converter completed successfully.')


if __name__ == "__main__":
    main()
