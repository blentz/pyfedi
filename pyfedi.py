# This file is part of PieFed, which is licensed under the GNU Affero General Public License (AGPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.
from app import create_app, cli

app = create_app()
cli.register(app)
