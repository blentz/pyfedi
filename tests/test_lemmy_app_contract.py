"""The Jerboa contract's generator (interop/apps/kotlin_models.py) and its checker (tests/lemmy_app_contract.py): a
model read from Kotlin source requires what kotlinx.serialization requires, and the checker names each way an answer
breaks it (spec 2026-11-10-lemmy-v3-design §2)."""
import pytest

from interop.apps import kotlin_models
from tests import lemmy_app_contract as contract

SOURCE = '''
package x

typealias PostId = Long

@Serializable
internal data class PostView(
    val post: Post,
    val subscribed: SubscribedType /* "Subscribed" | "NotSubscribed" */,
    val my_vote: Int = 0,
    val tags: List<String>,
)

@Serializable
internal data class Post(
    val id: PostId,
    val name: String,
    // a comment line
    val url: String? = null,
    val body: String?,
)

internal data class Unreached(val id: Long)
'''
ENUMS = '''
enum class SubscribedType {
    Subscribed,
    NotSubscribed,
    @SerialName("pending") Pending,
}

enum class SortType(override val minimumVersion: Version = MINIMUM_API_VERSION) : MinimumVersion {
    Hot,
    TopSixHour(V0_18_0),
    ;
}
'''


@pytest.fixture
def models(tmp_path):
    (tmp_path / 'datatypes').mkdir()
    (tmp_path / 'datatypes' / 'PostView.kt').write_text(SOURCE)
    (tmp_path / 'datatypes' / 'README.md').write_text('not Kotlin')
    (tmp_path / 'Enums.kt').write_text(ENUMS)
    return kotlin_models.reached(kotlin_models.read([str(tmp_path / 'datatypes')], str(tmp_path / 'Enums.kt')),
                                 ['PostView'])


def test_a_model_records_what_may_be_missing_and_what_may_be_null(models):
    post = {field['key']: field for field in models['classes']['Post']['fields']}
    assert post['id'] == {'key': 'id', 'type': 'Long', 'nullable': False, 'optional': False}   # the alias resolved
    assert post['url']['nullable'] and post['url']['optional']
    assert post['body']['nullable'] and not post['body']['optional']
    view = {field['key']: field for field in models['classes']['PostView']['fields']}
    assert view['my_vote']['optional'] and not view['my_vote']['nullable']
    assert view['subscribed']['type'] == 'SubscribedType' and view['tags']['type'] == 'List<String>'


def test_only_what_the_roots_reach_is_kept_with_every_enum(models):
    assert set(models['classes']) == {'PostView', 'Post'}
    assert models['enums'] == {'SubscribedType': ['Subscribed', 'NotSubscribed', 'pending'],
                               'SortType': ['Hot', 'TopSixHour']}


def test_the_checker_names_each_break_by_path():
    person = {'id': 1, 'name': 'a', 'banned': False, 'published': '2026-01-01T00:00:00Z', 'actor_id': 'x',
              'local': True, 'deleted': False, 'bot_account': False, 'instance_id': 1}
    assert contract.problems(person, 'Person') == []
    assert contract.problems({**person, 'display_name': None}, 'Person') == []   # nullable, defaulted
    broken = {**person, 'id': '1', 'banned': None, 'local': 1}
    del broken['name']
    assert contract.problems(broken, 'Person') == ['.id: \'1\', not Long', '.name: missing', '.banned: null',
                                                   '.local: 1, not Boolean']


def test_the_checker_reads_lists_numbers_enums_and_maps():
    assert contract.problems([1, True], 'List<Long>') == ['[1]: True, not Long']
    assert contract.problems('x', 'List<Long>') == [': not a list']
    assert contract.problems(1.5, 'Double') == [] and contract.problems('1', 'Float') == [': not a number']
    assert contract.problems('Hot', 'SortType') == [] and contract.problems('Best', 'SortType') == [": 'Best' is no SortType"]
    assert contract.problems({}, 'Map<String,String>') == [] and contract.problems([], 'Map<String,String>') != []
    assert contract.problems([], 'Person') == ['.: not an object (Person)']
    assert contract.problems(3, 'String') == [': 3, not String']


def test_a_type_outside_the_contract_is_a_mistake_in_the_test():
    with pytest.raises(KeyError):
        contract.problems({}, 'NoSuchModel')
