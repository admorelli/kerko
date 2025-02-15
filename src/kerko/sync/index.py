"""Update the search index from the local cache."""

import whoosh
from flask import current_app
from whoosh.query import Term

from kerko.extractors import ItemTitleExtractor
from kerko.shortcuts import composer, config
from kerko.storage import SchemaError, SearchIndexError, load_object, open_index, save_object
from kerko.tags import TagGate


def sync_index(full=False):  # noqa: ARG001
    """
    Build the search index from the local cache.

    Return the number of synchronized items.
    """
    current_app.logger.info("Starting index sync...")
    library_context = load_object("cache", "library")

    cache = open_index("cache")
    cache_item_version = load_object('cache', 'item-version', default=0)
    index_item_version = load_object('index', 'item-version', default=0)
    cache_collection_version = load_object('cache', 'collection-version')
    index_collection_version = load_object('index', 'collection-version')

    # FIXME: The following does not detect when just the collections have changed in the cache
    #        (with no item changes). Should check the collections_version!
    #        https://pyzotero.readthedocs.io/en/latest/#zotero.Zotero.collection_versions
    if not cache_item_version:
        msg = "The cache is empty and needs to be synchronized first."
        raise SearchIndexError(msg)

    if (
            not full
            and index_item_version == cache_item_version
            and index_collection_version == cache_collection_version):
        current_app.logger.warning(
            f"The index is already up-to-date with cache version {cache_item_version}, nothing to do."
        )
        return 0

    def yield_items(parent_key):
        with cache.searcher() as searcher:
            results = searcher.search(Term("parentItem", parent_key), limit=None)
            for hit in results:
                yield hit.fields()

    def yield_top_level_items():
        return yield_items("")

    def yield_children(parent):
        return yield_items(parent["key"])

    count = 0
    index = open_index("index", schema=composer().schema, auto_create=True, write=True)
    writer = index.writer(
        limitmb=config("kerko.performance.whoosh_index_memory_limit"),
        procs=config("kerko.performance.whoosh_index_processors"),
    )
    try:
        writer.mergetype = whoosh.writing.CLEAR
        gate = TagGate(
            config("kerko.zotero.item_include_re"),
            config("kerko.zotero.item_exclude_re"),
        )
        for item in yield_top_level_items():
            count += 1
            if gate.check(item["data"]):
                item["children"] = list(yield_children(item))  # Extend the base Zotero item dict.
                document = {}
                for spec in list(composer().fields.values()) + list(composer().facets.values()):
                    spec.extract_to_document(document, item, library_context)
                writer.update_document(**document)
                current_app.logger.debug(
                    f"Item {count} updated ({item['key']}, {item.get('itemType')}): "
                    f"{ItemTitleExtractor().extract(item, library_context, None)}"
                )
            else:
                current_app.logger.debug(f"Item {count} excluded ({item['key']})")
    except (whoosh.fields.FieldConfigurationError, whoosh.fields.UnknownFieldError) as e:
        writer.cancel()
        current_app.logger.error(e)
        msg = "Schema changes are required. Please clean index."
        raise SchemaError(msg) from e
    except Exception:
        writer.cancel()
        raise
    else:
        writer.commit()
        # Save the cache's last_modified timestamp. Later, we cannot access the
        # cache directly to show the user when the data was last synchronized
        # from Zotero, because there is no guarantee that what the user's sees
        # (i.e., the current content of the index) is still in sync with the
        # cache (the cache might have been cleaned, or it might have been just
        # updated, with an index update still pending).
        save_object("index", "last_update_from_zotero", cache.last_modified())
        save_object('index', 'item-version', cache_item_version)
        save_object('index', 'collection-version', cache_collection_version)
        current_app.logger.info(
            f"Index sync successful, now at version {cache_item_version} "
            f"({count} top level item(s) processed)."
        )
    return count
