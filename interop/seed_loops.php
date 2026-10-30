<?php
// Seed the harness Loops (run.sh pipes this into `php artisan tinker`): admin settings with federation on, OAuth keys,
// a personal access client, user lucy, remote video allowed from piefed.test, and lucy's API token on the last line as
// TOKEN=<token>. Safe to repeat.
if (! App\Models\AdminSetting::where('key', 'federation.enableFederation')->exists()) {
    Artisan::call('db:seed', ['--class' => 'AdminSettingsSeeder', '--force' => true]);
}
// Set directly, not through the admin settings API: enabling federation there also submits this server to the public
// Loops server list (AdminSettingsController::handleSubmitToServerList)
App\Models\AdminSetting::set('federation.enableFederation', true, 'boolean');
app(App\Services\ConfigService::class)->federation(true);
(new App\Services\SettingsFileService)->flush();
if (! file_exists(storage_path('oauth-private.key'))) {
    Artisan::call('passport:keys');
}
// As seed_pixelfed.php: a personal client whose id could equal a user id makes Passport refuse that user's tokens
if (! DB::table('oauth_clients')->where('id', 100)->exists()) {
    Artisan::call('passport:client', ['--personal' => true, '--name' => 'interop', '--provider' => 'users', '--no-interaction' => true]);
    DB::table('oauth_clients')->where('id', DB::table('oauth_clients')->max('id'))->update(['id' => 100]);
}
// Every Loops server refuses remote video until its admin allows the sending instance (CreateValidator.php:164)
App\Models\Instance::updateOrCreate(['domain' => 'piefed.test'], ['allow_video_posts' => true, 'allow_videos_in_fyf' => true]);
// Subscribe to the stand-in relay (interop/relay_standin.py), as an admin's Relays -> Add relay does, sending
// public posts to it and taking its Announces
if (! App\Models\RelaySubscription::where('relay_url', 'https://relay.test')->exists()) {
    app(App\Services\RelayService::class)->subscribe('https://relay.test')
        ->update(['send_public_posts' => true, 'receive_content' => true]);
}
$lucy = App\Models\User::whereUsername('lucy')->first();
if (! $lucy) {
    $lucy = new App\Models\User;
    $lucy->name = 'lucy';
    $lucy->username = 'lucy';
    $lucy->email = 'lucy@loops.test';
    $lucy->password = Hash::make('interop-lucy-pw');
    $lucy->email_verified_at = now();
    $lucy->is_admin = false;
    $lucy->save();   // UserObserver::created makes the profile
}
echo 'TOKEN='.$lucy->createToken('interop')->accessToken."\n";
