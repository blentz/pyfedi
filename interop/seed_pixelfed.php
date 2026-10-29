<?php
// Seed the harness Pixelfed (run.sh pipes this into `php artisan tinker`): OAuth keys, the instance actor, a personal
// access client, user erin, and erin's API token on the last line as TOKEN=<token>. Safe to repeat.
if (! file_exists(storage_path('oauth-private.key'))) {
    Artisan::call('passport:keys');
}
Artisan::call('instance:actor');
// Passport's TokenGuard takes a token whose user id equals its client id for a client-credentials token when the
// client allows that grant (vendor/laravel/passport/src/Guards/TokenGuard.php:146), and a personal client does. The
// first client and the first user are both id 1, so every token of erin's would be refused: renumber the client to
// 100, above any harness user id.
if (! DB::table('oauth_clients')->where('id', 100)->exists()) {
    Artisan::call('passport:client', ['--personal' => true, '--name' => 'interop', '--provider' => 'users', '--no-interaction' => true]);
    DB::table('oauth_clients')->where('id', DB::table('oauth_clients')->max('id'))->update(['id' => 100]);
}
$erin = App\Models\User::whereUsername('erin')->first();
if (! $erin) {
    Artisan::call('user:create', ['--name' => 'erin', '--username' => 'erin', '--email' => 'erin@pixelfed.test',
        '--password' => 'interop-erin-pw', '--confirm_email' => 1, '--no-interaction' => true]);
    $erin = App\Models\User::whereUsername('erin')->first();
}
echo 'TOKEN='.$erin->createToken('interop', ['read', 'write', 'follow', 'push'])->accessToken."\n";
