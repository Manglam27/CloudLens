import 'package:flutter/material.dart';
import 'package:amplify_auth_cognito/amplify_auth_cognito.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:cloud_lens/Pages/confirm_signup.dart';
import 'package:cloud_lens/Pages/main_page.dart'; // Assuming you're navigating here on successful signup
import 'package:cloud_lens/Pages/login.dart';

class SignupPage extends StatefulWidget {
  final Future<void> Function(BuildContext) signOutCallback;

  const SignupPage({super.key, required this.signOutCallback});

  @override
  _SignupPageState createState() => _SignupPageState();
}

class _SignupPageState extends State<SignupPage> {
  final TextEditingController emailController = TextEditingController();
  final TextEditingController passwordController = TextEditingController();

  bool _isSigningUp = false;

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Sends the user to the page that collects the emailed verification code.
  void _goToConfirmation(String email, String password) {
    Navigator.pushReplacement(
      context,
      MaterialPageRoute(
        builder: (context) => ConfirmSignupPage(
          signOutCallback: widget.signOutCallback,
          email: email,
          password: password,
        ),
      ),
    );
  }

  Future<void> signUp(String email, String password) async {
    setState(() => _isSigningUp = true);
    try {
      final result = await Amplify.Auth.signUp(
        username: email,
        password: password,
        options: SignUpOptions(
          userAttributes: {
            CognitoUserAttributeKey.email: email,
          },
        ),
      );

      if (!mounted) return;

      if (result.isSignUpComplete) {
        // The pool does not require verification, so the account is usable now.
        Navigator.pushReplacement(
          context,
          MaterialPageRoute(builder: (context) => MainPage(signOutCallback: widget.signOutCallback)),
        );
        return;
      }

      // The pool requires email verification. Cognito has emailed a code and
      // the account stays UNCONFIRMED until it is submitted, so collect it.
      if (result.nextStep.signUpStep == AuthSignUpStep.confirmSignUp) {
        _goToConfirmation(email, password);
      } else {
        _showMessage('Sign up needs an extra step: ${result.nextStep.signUpStep.name}');
      }
    } on UsernameExistsException {
      // An account exists. If it was never confirmed the user is stuck, so
      // resend the code and let them finish verifying instead of dead-ending.
      try {
        await Amplify.Auth.resendSignUpCode(username: email);
        if (!mounted) return;
        _showMessage('This account already exists but is not verified. We sent a new code.');
        _goToConfirmation(email, password);
      } on AuthException {
        _showMessage('An account with this email already exists. Please log in.');
      }
    } on InvalidPasswordException catch (e) {
      _showMessage('Password does not meet the requirements: ${e.message}');
    } on AuthException catch (e) {
      _showMessage('Signup failed: ${e.message}');
    } finally {
      if (mounted) setState(() => _isSigningUp = false);
    }
  }

  void navigateToSignInPage() {
    Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => LoginPage(signOutCallback: widget.signOutCallback)),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      resizeToAvoidBottomInset: false,
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        title: const Text('Signup'),
        backgroundColor: Colors.white,
        elevation: 0,
      ),
      body: Stack(
        children: [
          // Gradient background
          Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [
                  Color(0xFF8EC5FC), // Light blue
                  Color(0xFFE0C3FC), // Soft purple
                ],
              ),
            ),
          ),
          // Foreground content
          Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 20.0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.center,
                children: [
                  // Page icon
                  Container(
                    margin: const EdgeInsets.symmetric(vertical: 30.0),
                    child: Image.asset(
                      'assets/icon.png',
                      height: 175.0,
                      width: 175.0,
                    ),
                  ),
                  // Email input
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 16.0),
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(30.0),
                      boxShadow: [
                        BoxShadow(
                          color: Colors.black.withOpacity(0.1),
                          spreadRadius: 2,
                          blurRadius: 5,
                          offset: const Offset(0, 2),
                        )
                      ],
                    ),
                    child: TextField(
                      controller: emailController,
                      decoration: const InputDecoration(
                        icon: Icon(Icons.email),
                        hintText: "Email",
                        border: InputBorder.none,
                      ),
                    ),
                  ),
                  const SizedBox(height: 20.0),
                  // Password input
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 16.0),
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(30.0),
                      boxShadow: [
                        BoxShadow(
                          color: Colors.black.withOpacity(0.1),
                          spreadRadius: 2,
                          blurRadius: 5,
                          offset: const Offset(0, 2),
                        )
                      ],
                    ),
                    child: TextField(
                      controller: passwordController,
                      obscureText: true,
                      decoration: const InputDecoration(
                        icon: Icon(Icons.lock),
                        hintText: "Password",
                        border: InputBorder.none,
                      ),
                    ),
                  ),
                  const SizedBox(height: 30),
                  // Sign Up button
                  ElevatedButton(
                    onPressed: _isSigningUp
                        ? null
                        : () {
                            String email = emailController.text.trim();
                            String password = passwordController.text.trim();

                            if (email.isEmpty || password.isEmpty) {
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(content: Text('Please fill in both fields')),
                              );
                              return;
                            }

                            signUp(email, password);
                          },
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.deepPurple,
                      foregroundColor: Colors.white,
                      elevation: 5.0,
                      minimumSize: const Size(double.infinity, 45),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(30),
                      ),
                    ),
                    child: _isSigningUp
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                              color: Colors.white,
                            ),
                          )
                        : const Text(
                            'Sign Up',
                            style: TextStyle(fontSize: 16.0, fontWeight: FontWeight.bold),
                          ),
                  ),
                  const SizedBox(height: 15.0),
                  // Divider
                  Row(
                    children: [
                      Expanded(
                        child: Container(
                          margin: const EdgeInsets.only(left: 10.0, right: 20.0),
                          child: const Divider(color: Colors.deepPurple),
                        ),
                      ),
                      const Text(
                        "OR",
                        style: TextStyle(color: Colors.deepPurple),
                      ),
                      Expanded(
                        child: Container(
                          margin: const EdgeInsets.only(left: 20.0, right: 10.0),
                          child: const Divider(color: Colors.deepPurple),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 15.0),
                  // Login button
                  ElevatedButton(
                    onPressed: navigateToSignInPage,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.deepPurple,
                      foregroundColor: Colors.white,
                      elevation: 5.0,
                      minimumSize: const Size(double.infinity, 45),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(30),
                      ),
                    ),
                    child: const Text(
                      'Login',
                      style: TextStyle(fontSize: 16.0, fontWeight: FontWeight.bold),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
