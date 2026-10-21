import 'package:flutter/material.dart';
import 'package:cloud_lens/amplifyconfiguration.dart';
import 'package:cloud_lens/Pages/login.dart';
import 'package:cloud_lens/Pages/main_page.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:amplify_auth_cognito/amplify_auth_cognito.dart';
import 'package:amplify_storage_s3/amplify_storage_s3.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Amplify.addPlugins([AmplifyAuthCognito(), AmplifyStorageS3()]);
  await Amplify.configure(amplifyconfig);
  runApp(MyApp());
}

class MyApp extends StatefulWidget {
  const MyApp({super.key});
  
  @override
  _MyAppState createState() => _MyAppState();
}

  class _MyAppState extends State<MyApp> with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    // Remove observer when the app is disposed
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  // Sign out when the app is backgrounded or closed
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) async {
    if (state == AppLifecycleState.detached) {
      // Sign out the user when the app is detached (closed)
      try {
        await Amplify.Auth.signOut();
        print('User signed out successfully');
      } catch (e) {
        print('Error signing out: $e');
      }
    }
  }

  // Checked once, not on every rebuild, and given up on after a while so the
  // app falls back to the login page instead of spinning forever.
  late final Future<bool> _signedIn = _isUserSignedIn();

  Future<bool> _isUserSignedIn() async {
    try {
      await Amplify.Auth.getCurrentUser().timeout(const Duration(seconds: 10));
      return true;
    } catch (e) {
      return false;
    }
  }

  // Sign-out function
  Future<void> _signOut(BuildContext context) async {
    try {
      await Amplify.Auth.signOut();
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(builder: (context) => LoginPage(signOutCallback: _signOut)), // Navigate to Login Page after sign-out
      );
    } catch (e) {
      print('Error signing out: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Cloud Lens',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.deepPurple),
        useMaterial3: true,
      ),
      home: FutureBuilder<bool>(
        future: _signedIn,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Scaffold(body: Center(child: CircularProgressIndicator()));
          }
          return snapshot.data == true
              ? MainPage(signOutCallback: _signOut) // Pass the sign-out function
              : LoginPage(signOutCallback: _signOut);
        },
      ),
    );
  }
}