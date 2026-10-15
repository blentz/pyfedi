# Seed the harness Mastodon: user bob and an API token for him. Idempotent.
account = Account.find_local('bob')
unless account
  owner = User.create!(email: 'bob@mastodon.test', password: 'interop-bob-pw', agreement: true,
                       confirmed_at: Time.now.utc, approved: true, account_attributes: { username: 'bob' })
  account = owner.account
end
# approved: true on create is only a registration hint; with approval-required registration the login stays pending
account.user.approve! unless account.user.approved?
app = Doorkeeper::Application.find_or_create_by!(name: 'interop') do |a|
  a.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
  a.scopes = 'read write follow'
end
token = Doorkeeper::AccessToken.create!(application: app, resource_owner_id: account.user.id, scopes: 'read write follow')
puts token.token
