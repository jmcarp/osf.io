# -*- coding: utf-8 -*-

from __future__ import division

import re
import copy
import math
import logging
from requests.exceptions import ConnectionError

import pyelasticsearch

from framework import sentry

from website import settings
from website.filters import gravatar
from website.models import User, Node
from website.search import exceptions

logger = logging.getLogger(__name__)


# These are the doc_types that exist in the search database
TYPES = ['website/project', 'website/component', 'website/registration', 'website/user']
ALIASES = {
    'website/project': 'projects',
    'website/component': 'components',
    'website/registration': 'registrations',
    'website/user': 'users'
}

try:
    elastic = pyelasticsearch.ElasticSearch(
        settings.ELASTIC_URI,
        timeout=settings.ELASTIC_TIMEOUT
    )
    logging.getLogger('pyelasticsearch').setLevel(logging.WARN)
    logging.getLogger('requests').setLevel(logging.WARN)
    elastic.health()
except ConnectionError as e:
    sentry.log_exception()
    sentry.log_message("The SEARCH_ENGINE setting is set to 'elastic', but there "
            "was a problem starting the elasticsearch interface. Is "
            "elasticsearch running?")
    elastic = None


def requires_search(func):
    def wrapped(*args, **kwargs):
        if elastic is not None:
            try:
                return func(*args, **kwargs)
            except pyelasticsearch.exceptions.ElasticHttpNotFoundError as e:
                raise exceptions.IndexNotFoundError(e.error)
            except pyelasticsearch.exceptions.ElasticHttpError as e:
                if 'ParseException' in e.error:
                    raise exceptions.MalformedQueryError(e.error)
                raise exceptions.SearchException(e.error)

        sentry.log_message('Elastic search action failed. Is elasticsearch running?')
    return wrapped


@requires_search
def get_counts(count_query, index):
    counts = {}
    try:
        count_query['query']['filtered']['query']['query_string']['query'] = \
            re.sub(r' AND category:\S*', '', count_query['query']['filtered']['query']['query_string']['query'])
    #TODO: Overly broad exception needs fixing
    except Exception:
        pass

    if count_query.get('from') is not None:
        del count_query['from']
    if count_query.get('size')is not None:
        del count_query['size']
    if count_query.get('sort'):
        del count_query['sort']
    for _type in TYPES:
        try:
            if len(_type.split('/')) > 1:
                count_index, count_type = _type.split('/')
            else:
                count_index, count_type = index, _type
            count = elastic.count(count_query, index=count_index, doc_type=count_type)['count']
        #TODO: Overly broad exception needs fixing
        except Exception:
            count = 0
        counts[ALIASES.get(_type, _type)] = count
    # Figure out which count we should display as a total
    counts['total'] = sum([counts[key] for key in counts.keys()])
    return counts

@requires_search
def get_tags(query, index):
    query['aggregations'] = {
        'tag_cloud': {
            'terms': {'field': "tags"}
        }
    }
    if query.get('from') is not None:
        del query['from']
    if query.get('size')is not None:
        del query['size']
    if query.get('sort'):
        del query['sort']

    try:
        results = elastic.search(query, index=index, doc_type='_all')
        tags = results['aggregations']['tag_cloud']['buckets']
    #TODO: Overly broad exception needs fixing
    except Exception:
        tags = []

    return tags

@requires_search
def search(query, index='website', search_type='_all'):

    # Get document counts by type
    counts = get_counts(copy.deepcopy(query), index)
    tags = get_tags(copy.deepcopy(query), index)

    # Run the real query and get the results
    raw_results = elastic.search(query, index=index, doc_type=search_type)

    results = [hit['_source'] for hit in raw_results['hits']['hits']]
    return_value = {
        'results': format_results(results),
        'counts': counts,
        'tags': tags,
        'typeAliases': {
            'components': 'component',
            'projects': 'project',
            'registrations': 'registration',
            'users': 'user',
        }
    }
    return return_value


def format_results(results):
    ret = []
    for result in results:
        if result.get('category') == 'user':
            result['url'] = '/profile/' + result['id']
        elif result.get('category') in {'project', 'component', 'registration'}:
            result = format_result(result, result.get('parent_id'))
        ret.append(result)
    return ret


def format_result(result, parent_id=None):
    parent_info = load_parent(parent_id)
    formatted_result = {
        'contributors': result['contributors'],
        'wiki_link': result['url'] + 'wiki/',
        'title': result['title'],
        'url': result['url'],
        'is_component': False if parent_info is None else True,
        'parent_title': parent_info.get('title') if parent_info is not None else None,
        'parent_url': parent_info.get('url') if parent_info is not None else None,
        'tags': result['tags'],
        'contributors_url': result['contributors_url'],
        'is_registration': (result['is_registration'] if parent_info is None
                                                        else parent_info.get('is_registration')),
        'description': result['description'] if parent_info is None else None,
        'category': result.get('category')
    }

    return formatted_result


def load_parent(parent_id):
    parent = Node.load(parent_id)
    if parent is None:
        return None
    parent_info = {}
    if parent is not None and parent.is_public:
        parent_info['title'] = parent.title
        parent_info['url'] = parent.url
        parent_info['is_registration'] = parent.is_registration
        parent_info['id'] = parent._id
    else:
        parent_info['title'] = '-- private project --'
        parent_info['url'] = ''
        parent_info['is_registration'] = None
        parent_info['id'] = None
    return parent_info


@requires_search
def update_node(node, index='website'):
    from website.addons.wiki.model import NodeWikiPage

    component_categories = ['', 'hypothesis', 'methods and measures', 'procedure', 'instrumentation', 'data', 'analysis', 'communication', 'other']
    category = 'component' if node.category in component_categories else node.category

    if category == 'project':
        elastic_document_id = node._id
        parent_id = None
        category = 'registration' if node.is_registration else category
    else:
        try:
            elastic_document_id = node._id
            parent_id = node.parent_id
            category = 'registration' if node.is_registration else category
        except IndexError:
            # Skip orphaned components
            return
    if node.is_deleted or not node.is_public:
        delete_doc(elastic_document_id, node)
    else:
        elastic_document = {
            'id': elastic_document_id,
            'contributors': [
                x.fullname for x in node.visible_contributors
                if x is not None
                and x.is_active()
            ],
            'contributors_url': [
                x.profile_url for x in node.visible_contributors
                if x is not None
                and x.is_active()
            ],
            'title': node.title,
            'category': category,
            'public': node.is_public,
            'tags': [tag._id for tag in node.tags if tag],
            'description': node.description,
            'url': node.url,
            'is_registration': node.is_registration,
            'registered_date': str(node.registered_date)[:10],
            'wikis': {},
            'parent_id': parent_id,
            'iso_timestamp': node.date_created,
            'boost': int(not node.is_registration) + 1,  # This is for making registered projects less relevant
        }
        for wiki in [
            NodeWikiPage.load(x)
            for x in node.wiki_pages_current.values()
        ]:
            elastic_document['wikis'][wiki.page_name] = wiki.raw_text(node)

        try:
            elastic.update(index, category, id=elastic_document_id, doc=elastic_document, upsert=elastic_document, refresh=True)
        except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
            elastic.index(index, category, elastic_document, id=elastic_document_id, overwrite_existing=True, refresh=True)


def generate_social_links(social):
    social_links = {}
    if 'github' in social and social['github']:
        social_links['github'] = 'http://github.com/{}'.format(social['github'])
    if 'impactStory' in social and social['impactStory']:
        social_links['impactStory'] = 'https://impactstory.org/{}'.format(social['impactStory'])
    if 'linkedIn' in social and social['linkedIn']:
        social_links['linkedIn'] = 'https://www.linkedin.com/profile/view?id={}'.format(social['linkedIn'])
    if 'orcid' in social and social['orcid']:
        social_links['orcid'] = 'http://orcid.com/{}'.format(social['orcid']),
    if 'personal' in social and social['personal']:
        social_links['personal'] = social['personal']
    if 'researcherId' in social and social['researcherId']:
        social_links['researcherId'] = 'http://researcherid.com/rid/{}'.format(social['researcherId'])
    if 'scholar' in social and social['scholar']:
        social_links['scholar'] = 'http://scholar.google.com/citations?user={}'.format(social['scholar'])
    if 'twitter' in social and social['twitter']:
        social_links['twitter'] = 'http://twitter.com/{}'.format(social['twitter'])
    return social_links

@requires_search
def update_user(user):
    if not user.is_active():
        try:
            elastic.delete('website', 'user', user._id, refresh=True)
            logger.debug('User ' + user._id + ' successfully removed from the Elasticsearch index')
            return
        except pyelasticsearch.exceptions.ElasticHttpNotFoundError as e:
            logger.error(e)
            return

    user_doc = {
        'id': user._id,
        'user': user.fullname,
        'job': user.jobs[0]['institution'] if user.jobs else '',
        'job_title': user.jobs[0]['title'] if user.jobs else '',
        'school': user.schools[0]['institution'] if user.schools else '',
        'category': 'user',
        'degree': user.schools[0]['degree'] if user.schools else '',
        'social': generate_social_links(user.social),
        'boost': 2,  # TODO(fabianvf): Probably should make this a constant or something
    }

    try:
        elastic.update('website', 'user', doc=user_doc, id=user._id, upsert=user_doc, refresh=True)
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
        elastic.index("website", "user", user_doc, id=user._id, overwrite_existing=True, refresh=True)


@requires_search
def delete_all():
    try:
        elastic.delete_index('website')
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError as e:
        logger.error(e)
        logger.error("The index 'website' was not deleted from elasticsearch")


@requires_search
def create_index():
    '''Creates index with some specified mappings to begin with,
    all of which are applied to all projects, components, and registrations'''
    mapping = {
        'properties': {
            'tags': {
                'type': 'string',
                'index': 'not_analyzed',
            }
        }
    }
    try:
        elastic.create_index('website')
        for type_ in ['project', 'component', 'registration']:
            elastic.put_mapping('website', type_, mapping)
    except pyelasticsearch.exceptions.IndexAlreadyExistsError:
        pass


@requires_search
def delete_doc(elastic_document_id, node, index='website'):
    category = 'registration' if node.is_registration else node.project_or_component
    try:
        elastic.delete(index, category, elastic_document_id, refresh=True)
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
        logger.warn("Document with id {} not found in database".format(elastic_document_id))


@requires_search
def search_contributor(query, page=0, size=10, exclude=None, current_user=None):
    """Search for contributors to add to a project using elastic search. Request must
    include JSON data with a "query" field.

    :param query: The substring of the username to search for
    :param page: For pagination, the page number to use for results
    :param size: For pagination, the number of results per page
    :param exclude: A list of User objects to exclude from the search
    :param current_user: A User object of the current user

    :return: List of dictionaries, each containing the ID, full name,
        most recent employment and education, gravatar URL of an OSF user

    """
    start = (page * size)
    query.replace(" ", "_")
    query = re.sub(r'[\-\+]', '', query)
    query = re.split(r'\s+', query)
    bool_filter = {
        'must': [],
        'should': [],
        'must_not': [],
    }
    if exclude is not None:
        for excluded in exclude:
            bool_filter['must_not'].append({
                'term': {
                    'id': excluded._id
                }
            })

    if len(query) > 1:
        for item in query:
            bool_filter['must'].append({
                'prefix': {
                    'user': item.lower()
                }
            })
    else:
        bool_filter['must'].append({
            'prefix': {
                'user': query[0].lower()
            }
        })

    query = {
        'query': {
            'filtered': {
                'filter': {
                    'bool': bool_filter
                }
            }
        },
        'from': start,
        'size': size,
    }

    results = elastic.search(query, index='website')
    docs = [hit['_source'] for hit in results['hits']['hits']]
    pages = math.ceil(results[u'hits'][u'total'] / size)

    users = []
    for doc in docs:
        # TODO: use utils.serialize_user
        user = User.load(doc['id'])

        if current_user:
            n_projects_in_common = current_user.n_projects_in_common(user)
        else:
            n_projects_in_common = 0

        if user is None:
            logger.error('Could not load user {0}'.format(doc['id']))
            continue
        if user.is_active():  # exclude merged, unregistered, etc.
            current_employment = None
            education = None

            if user.jobs:
                current_employment = user.jobs[0]['institution']

            if user.schools:
                education = user.schools[0]['institution']

            users.append({
                'fullname': doc['user'],
                'id': doc['id'],
                'employment': current_employment,
                'education': education,
                'n_projects_in_common': n_projects_in_common,
                'gravatar_url': gravatar(
                    user,
                    use_ssl=True,
                    size=settings.GRAVATAR_SIZE_ADD_CONTRIBUTOR,
                ),
                'profile_url': user.profile_url,
                'registered': user.is_registered,
                'active': user.is_active()

            })

    return \
        {
            'users': users,
            'total': results[u'hits'][u'total'],
            'pages': pages,
            'page': page,
        }
